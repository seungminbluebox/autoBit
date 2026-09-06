# OCI 모의매매 상시 운영·배포 설계

작성일: 2026-09-05
상태: 대화에서 승인된 OCI 네이티브 운영 방향을 구현 계약으로 문서화. 이 문서 검토가 끝난 뒤 별도 구현 계획을 작성한다.

## 1. 목적과 범위

현재 공통 전략 엔진을 사용하는 `KRW-BTC` 모의매매를 기존 OCI ARM64 Ubuntu 인스턴스에서 24시간 자동 실행한다. 프로세스 충돌, 서비스 재시작, 서버 재부팅, 새 코드 배포 뒤에도 같은 SQLite 원장과 공개 캔들 증거를 이어서 사용하고, 재시작 전후 로그를 조회할 수 있어야 한다.

이 설계는 모의매매 운영만 배포한다. 고정 잠금된 실전 진입점은 그대로 유지하고, 거래소 키·개인 API·실제 주문·실제 자금은 배포하지 않는다. 수익을 보장하거나 매수 시점을 추천하는 기능도 아니다. 백테스트와 실전 연결 코드는 같은 저장소에 남지만 OCI 상시 서비스가 실행하는 명령은 `paper-run` 하나뿐이다.

인프라 식별자, 공인·사설 IP, SSH 개인키 위치와 호스트키 지문은 공개 저장소에 기록하지 않는다. 배포 대상은 운영자가 로컬에서 관리하는 SSH 호스트 별칭으로 지정한다. 복구용 VM, `rpcbind`, VCN·Security List 변경, 인스턴스 Shape·Boot Volume 변경은 이 작업 범위 밖이다.

## 2. 검토한 배포 방식과 결정

### 채택: 네이티브 Python 3.12 + systemd

기존 서버에 컨테이너 계층을 추가하지 않고, 정확히 고정한 ARM64 Python 3.12 런타임과 잠금 파일로 의존성을 설치한다. `systemd`가 `paper-run`을 부팅 시 시작하고 프로세스 종료 시 다시 시작한다. 현재 서버의 자원 규모에서 구성 요소가 가장 적고, 기존 OS 기능으로 자동 시작·종료·로그·권한 격리를 함께 처리할 수 있다.

### 보류: Docker 또는 Podman

컨테이너는 런타임 재현성과 이미지 롤백이 편하지만 현재 서버에 설치되어 있지 않다. 엔진·이미지 저장 공간, 데몬 운영, ARM64 이미지 빌드와 취약점 갱신이라는 별도 운영 책임이 생긴다. 단일 Python 서비스에는 이 비용이 이점보다 크므로 도입하지 않는다.

### 제외: cron으로 반복 실행

현재 `paper-run`은 확정 4시간봉 경계, 재시도, 중복 처리 방지와 안전한 종료를 자체 관리하는 장기 실행 프로세스다. cron은 실행 중첩과 종료 시점 관리를 추가로 해결해야 하며, 프로세스 충돌 직후 복구와 로그 수집도 systemd보다 불리하다. 핵심 러너에는 cron을 사용하지 않는다.

## 3. 확인된 제약과 불변 조건

- 배포 대상은 Ubuntu 22.04 ARM64이며 1 OCPU와 6 GB RAM을 사용한다. 현재 용량을 늘리지 않는다.
- OS 기본 Python 3.10은 프로젝트 계약 `>=3.12,<3.13`과 호환되지 않는다. OS Python을 교체하거나 덮어쓰지 않는다.
- Docker와 Podman은 설치하지 않는다. 기존 `git`, `curl`, `systemd`를 활용한다.
- 서버 시간대는 UTC로 유지한다. 전략과 스케줄러도 UTC 확정봉을 기준으로 하므로 시스템 시간대를 변경하거나 cron 시간대에 의존하지 않는다.
- 알려진 기존 애플리케이션 트리 `/home/ubuntu/autoBit`와 그 안의 `.env`는 읽기·source·복사·이동·수정·권한 변경·삭제하지 않는다. 새 서비스는 `ProtectHome=true`로 홈 디렉터리 접근 자체를 차단한다.
- 새 수신 포트를 열지 않는다. 서비스는 공개 업비트 캔들 HTTPS 요청을 위한 outbound 연결만 사용한다.
- 단 하나의 `paper-run` 프로세스만 같은 원장을 쓴다. systemd 서비스와 배포 잠금이 두 번째 실행자를 만들지 못하게 한다.
- 서비스나 배포 스크립트는 `live` 명령을 실행하거나 실전 잠금을 해제하는 옵션을 제공하지 않는다.

## 4. 서버 디렉터리와 소유권

| 경로 | 역할 | 소유권과 변경 규칙 |
| --- | --- | --- |
| `/opt/autobit/releases/` 아래의 40자리 소문자 hex 커밋 이름 | 커밋별 코드, 고정 런타임, `.venv` | `root:root`; 준비 완료 후 읽기 전용이며 서비스 사용자는 수정하지 못한다. |
| `/opt/autobit/current` | 현재 릴리스 심볼릭 링크 | `root`만 원자적으로 교체한다. |
| `/opt/autobit/tools/` | 검증된 배포 도구와 런타임 부트스트랩 | 버전과 SHA-256이 고정된 파일만 둔다. |
| `/var/lib/autobit/paper/paper.sqlite3` | 모의 계좌·주문·체결·위험·상태 원장 | `autobit:autobit`, 디렉터리 모드 `0700`; 릴리스와 분리하여 영속 보존한다. |
| `/var/lib/autobit/raw/paper/` | 공개 캔들 원본·checkpoint·manifest | `autobit:autobit`, 디렉터리 모드 `0700`; 재배포 때 삭제하지 않는다. |
| `/var/backups/autobit/paper/` | 배포 직전 원장 묶음과 해시 | `root:root`, 디렉터리 모드 `0700`; 자동 배포가 기존 백업을 덮어쓰지 않는다. |
| systemd journal | stdout·stderr 운영 로그 | 영속 journal을 사용하며 서비스가 직접 로그 파일을 관리하지 않는다. |

`autobit`은 로그인할 수 없는 전용 시스템 사용자와 그룹으로 만든다. 릴리스 코드는 쓸 수 없고 `/var/lib/autobit`만 쓸 수 있다. 배포 스크립트는 모든 대상 경로를 절대 경로로 정규화한 뒤 허용된 `/opt/autobit`, `/var/lib/autobit`, `/var/backups/autobit` 아래인지 검사한다. 빈 경로, `/`, `/home`, `/home/ubuntu/autoBit` 또는 그 하위 경로가 전달되면 변경 전에 실패한다.

## 5. 재현 가능한 ARM64 런타임

저장소에 `uv.lock`을 추가하고 운영 설치는 `uv sync --frozen --no-dev`와 동등한 고정 설치만 허용한다. 범위 지정 의존성을 배포 시점에 새로 해석하거나 `pip install -U`를 실행하지 않는다.

배포 자산에는 다음 값을 문자열 리터럴로 기록한다.

- `uv`의 정확한 버전, ARM64 Linux 배포물 URL과 SHA-256
- Python 3.12의 정확한 patch 버전과 관리형 ARM64 런타임 식별자
- 허용 Python 범위가 아니라 실제 선택된 인터프리터 버전

구현 시점의 공식 안정 릴리스 중 프로젝트 의존성이 설치되고 테스트되는 조합을 한 번 선택해 위 값을 고정한다. `latest` URL, 변경 가능한 태그, 버전 범위, `curl | sh`는 사용하지 않는다. 다운로드 파일의 SHA-256이 저장된 값과 다르면 압축 해제나 실행 전에 실패한다. 서버의 `/usr/bin/python3`와 OS 패키지는 교체하지 않는다.

고정 런타임 압축은 `root`의 제한적인 `umask` 아래에서 해제하되, 검증된 도구 트리는 서비스 사용자가 읽고 디렉터리를 통과할 수 있도록 `a+rX`를 적용하고 group/other 쓰기 권한은 제거한다. 이미 설치된 같은 버전의 도구는 archive hash, `root:root` 소유권, 쓰기 금지와 실제 버전을 먼저 검증한 뒤 동일한 권한 정규화를 다시 적용한다. 이 규칙은 부분 실패 뒤 재실행해도 `autobit` 사용자가 Python 실행 경로에 접근할 수 있게 한다.

새 릴리스의 `.venv`는 해당 릴리스 안에 생성한다. 빌드가 끝나면 다음 검사에 모두 성공해야 현재 릴리스 후보가 된다.

1. 인터프리터가 ARM64용 Python 3.12의 고정된 patch 버전인지 확인한다.
2. `uv.lock`이 변경되지 않은 `--frozen` 설치인지 확인한다.
3. `python -m autobit.cli --help`와 패키지 import smoke test를 통과한다.
4. 기존 운영 원장이 있으면 그 복사본에 `paper-status`를 실행해 읽기 전용 조회가 성공한다. 최초 설치라면 운영 경로와 분리된 커밋별 smoke 경로에서 `paper-once`로 새 시험 원장을 만들고 `paper-status`로 검증한다. 이 시험은 공개 캔들만 사용하고 실제 운영 원장을 만들거나 수정하지 않는다.

## 6. systemd 서비스 계약

서비스 이름은 `autobit-paper.service` 하나다. 핵심 실행 계약은 다음과 같다.

```text
WorkingDirectory=/opt/autobit/current
ExecStart=/opt/autobit/current/.venv/bin/python -m autobit.cli paper-run \
  --db /var/lib/autobit/paper/paper.sqlite3 \
  --data-dir /var/lib/autobit/raw/paper
```

- `Wants=network-online.target`, `After=network-online.target`로 네트워크 준비 뒤 시작한다.
- `User=autobit`, `Group=autobit`, `UMask=0077`을 사용한다.
- `Restart=always`, `RestartSec=60s`, `StartLimitIntervalSec=0`으로 충돌과 일시적인 구성 실패 뒤에도 60초 간격으로 계속 재시도한다. 운영자가 명시적으로 `systemctl stop`한 경우에는 systemd 규칙대로 정지 상태를 유지한다.
- `KillSignal=SIGINT`, `TimeoutStopSec=120s`로 Python의 `KeyboardInterrupt` 경로와 SQLite `close()`가 실행될 시간을 준다. 제한 시간이 지나기 전에는 강제 종료하지 않는다.
- `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`를 적용하고 `/var/lib/autobit`만 `ReadWritePaths`로 연다. 실제 서버에서 `systemd-analyze verify`와 smoke test를 통과하지 못한 hardening 옵션은 조용히 제거하지 않고 원인을 문서화해 재검토한다.
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`로 로컬 SQLite와 공개 HTTPS에 필요한 주소 체계만 허용한다.
- 1 OCPU 환경의 과도한 수치 연산 스레드를 막기 위해 `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `NUMEXPR_NUM_THREADS=1`을 서비스 환경에 고정한다.
- 전용 사용자의 home은 `/var/lib/autobit`으로 두고 `HOME=/var/lib/autobit`, `XDG_CACHE_HOME=/var/lib/autobit/.cache`, `PYTHONDONTWRITEBYTECODE=1`을 고정한다. 라이브러리 캐시가 필요한 경우에만 영속 상태 트리 안에 쓰고 읽기 전용 릴리스에는 bytecode를 만들지 않는다.
- `.env` 또는 `EnvironmentFile`을 사용하지 않는다. Telegram 알림 자격증명도 이 최초 배포에는 넣지 않는다.
- `systemctl enable`로 부팅 자동 시작을 설정한다. 복구 가능한 공개 API 오류는 애플리케이션 내부 재시도가 담당하고, systemd 재시작은 프로세스 종료에 대한 두 번째 안전망이다.

원장이 손상되거나 검증할 수 없는 경우에는 새 빈 원장으로 자동 교체하거나 상태를 추측하지 않는다. 서비스는 재시작을 계속 시도하고 안전 오류만 기록하지만 거래 판단은 하지 않는다. “완전 자동 재개”는 검증된 기존 상태와 공개 데이터가 회복된다는 조건에서 적용되며, 상태 증거를 버리고 매매를 강행한다는 뜻이 아니다.

## 7. 로그 보존

stdout의 cycle JSON과 stderr의 안전한 오류 메시지는 systemd journal에만 기록한다. 토큰, 키, JWT, 인증 헤더, 원장 전체 내용과 예외 원문은 로그에 추가하지 않는다. 현재 paper 서비스는 공개 API만 사용하며 비밀을 주입하지 않는다.

재부팅 뒤에도 로그가 남도록 journald를 영속 모드로 구성한다. 이 설정은 서비스 하나가 아니라 서버 journal 전체에 영향을 주므로 다음 전역 상한을 명시적으로 적용하고 운영 문서에 그 영향을 알린다.

- `Storage=persistent`
- `SystemMaxUse=512M`
- `SystemKeepFree=5G`
- `MaxRetentionSec=90day`

설정 적용 전 기존 journald 구성 파일은 별도 파일로 백업하고 `/var/log/journal`의 존재·사용량·권한은 metadata로 기록한다. journal 데이터 트리 자체를 복제하지 않는다. 유효성 검사 뒤 journald를 reload 또는 restart하며, 기존 로그를 삭제하는 vacuum 명령은 실행하지 않는다. 운영 조회 명령은 `journalctl -u autobit-paper.service`를 사용한다.

## 8. 원자적 배포와 롤백

배포 명령은 정확한 40자리 Git 커밋 하나를 입력으로 받고 `flock`으로 동시 배포를 거부한다. 입력 커밋은 staging 시작 시 원격 `main`의 HEAD와 정확히 일치해야 한다. 작업 트리, 임의 브랜치, 아직 push되지 않은 커밋은 배포하지 않는다.

저장소에는 Windows 운영자용 PowerShell wrapper와 서버용 POSIX shell 설치기를 둔다. wrapper는 로컬 `origin`에서 원격 `main` HEAD를 읽어 입력 커밋과 비교하고, `git archive`로 추적 파일만 묶어 SHA-256 manifest와 함께 SSH 임시 디렉터리에 전송한다. 서버 설치기는 archive hash와 내부 배포 자산을 다시 확인한 뒤 staging을 시작한다. wrapper의 SSH host, user와 identity file은 실행 인자로만 받고 저장소 파일이나 journal에 저장하지 않는다. wrapper와 설치기는 개인키 파일을 직접 열거나 내용을 출력·복사하지 않으며, 표준 `ssh`·`scp` 프로세스에 인증용 파일 경로만 전달한다.

배포 순서는 다음과 같다.

1. 디스크 여유, 허용 경로, 기존 서비스 상태, wrapper가 검증한 원격 `main` 커밋 manifest와 서버 아키텍처를 읽기 전용으로 검사한다.
2. 실행 중인 기존 서비스에는 손대지 않고 새 커밋을 `/opt/autobit/releases/.staging-` 뒤에 40자리 커밋을 붙인 경로에 준비한다.
3. 고정 런타임 설치, lock 검증, import·CLI·원장 복사본 smoke test와 `systemd-analyze verify`를 통과시킨다.
4. staging을 최종 40자리 커밋 경로로 바꾸고 읽기 전용으로 만든다. 같은 커밋 릴리스가 이미 검증되어 있으면 재사용하고, 내용이 다르면 실패한다.
5. `autobit-paper.service`를 `SIGINT`로 정상 정지하고 inactive 상태와 단일 writer 종료를 확인한다.
6. 기존 운영 원장이 있으면 닫힌 `paper.sqlite3`와 존재하는 같은 시점의 `paper.sqlite3-wal`을 새 백업 디렉터리에 한 묶음으로 복사한다. `-shm`은 복사하지 않는다. 각 파일 SHA-256과 배포 전 `paper-status` 출력을 함께 저장하고 백업본을 다시 읽어 검증한다. 최초 설치로 원장이 없으면 DB·WAL·SHM이 모두 없음을 확인하고 `NO_EXISTING_LEDGER` manifest를 남긴다. DB 없이 WAL이나 SHM만 있으면 상태를 추측하지 않고 배포를 중단한다.
7. 임시 심볼릭 링크를 만든 뒤 같은 파일시스템에서 rename하여 `/opt/autobit/current`를 원자적으로 교체한다.
8. 필요한 unit 변경을 설치하고 daemon-reload한 뒤 서비스를 enable/start한다.
9. 서비스가 active인지, 프로세스가 단일 인스턴스인지, 같은 원장을 읽는 `paper-status`가 성공하는지, journal에 새 실행 기록이 생기는지 확인한다.

새 릴리스가 시작 검증에 실패하면 이전 심볼릭 링크로 원자적으로 되돌리고 이전 서비스를 다시 시작한다. 실제 원장은 자동 복원하지 않는다. 자동 복원은 새 릴리스가 이미 남긴 유효 이벤트를 지울 수 있기 때문이다. 모든 릴리스의 SQLite 변경은 이전 릴리스가 읽을 수 있는 비파괴·후방 호환 변경이어야 하며, 이 조건을 만족하지 않는 향후 DB 변경은 별도 마이그레이션·복원 설계 없이는 이 배포 경로에 넣지 않는다.

배포 직전 백업은 잘못된 배포나 수동 복원을 위한 증거다. 같은 Boot Volume에 있으므로 디스크 손실이나 OCI 인스턴스 회수에 대한 외부 백업은 아니다.

## 9. 장애와 자동 복구 동작

- 공개 API 일시 장애, stale candle과 데이터 재시도는 기존 `paper-run`의 SQLite evidence와 제한된 백오프를 그대로 사용한다. 프로세스는 종료하지 않고 자동 재평가한다.
- Python 프로세스가 종료되면 systemd가 60초 뒤 같은 릴리스와 같은 원장으로 다시 시작한다.
- 서버가 재부팅되면 enable된 서비스가 네트워크 준비 뒤 같은 경로로 시작한다.
- 디스크 부족, 원장 손상, 권한 오류 같은 안전하지 않은 상태에서는 빈 원장 생성, 파일 삭제, 실전 전환으로 우회하지 않는다. 재시도 로그와 기존 증거를 남긴다.
- 서비스의 자동 재시작과 전략의 `HALTED → REDUCED → NORMAL` 자동 회복은 서로 다른 기능이다. 전자는 프로세스를 살리고, 후자는 모든 데이터·원장 건강 조건이 충족됐을 때만 매매 판단을 다시 허용한다.
- 라이브 고정 잠금은 OCI 장애 복구와 무관하게 계속 적용된다.

## 10. 검증과 인수 시험

### 저장소에서 구현 중 수행

- 배포 경로 정규화와 legacy 홈 트리 거부 테스트
- 커밋 형식·원격 `main` 일치·동시 배포 잠금 테스트
- systemd unit의 실행 명령, 사용자, 재시작, 종료 신호, hardening, writable path 정적 계약 테스트
- 런타임 manifest의 exact-version·SHA-256·금지 패턴(`latest`, pipe-to-shell) 테스트
- 제한적인 `umask`로 해제된 신규·기존 런타임 도구가 `autobit` 사용자에게 읽기·디렉터리 통과 가능하고 쓰기 불가능한지 확인하는 Linux 권한 테스트
- `uv.lock` frozen 설치와 Python 3.12 import/CLI smoke test
- 백업 묶음, WAL 포함, `-shm` 제외, 해시·읽기 검증 테스트
- 배포 실패 시 이전 링크 복구와 실제 원장 무삭제 테스트
- 기존 paper 인수 시험, 세 환경 공통 전략 계약, 라이브 잠금·무자격증명·무개인요청 안전 시험
- 전체 테스트 suite 및 shell syntax 검사

### OCI 활성화 전에 수행

- 실제 ARM64에서 고정 Python·의존성 설치와 `systemd-analyze verify`
- 서비스 시작 전 시험용 임시 원장으로 smoke test
- 기존 legacy 홈 트리와 `.env`의 경로·크기·mtime·소유권 같은 metadata를 `stat`으로만 기록해 배포 전후 동일함을 검증한다. `.env` 내용은 열거나 해시하지 않는다.
- 새 수신 포트가 생기지 않았고 서비스 사용자에게 홈 접근 권한이 없음을 검증

### OCI 활성화 후 수행

1. 서비스를 정상 정지한 시점의 원장 상태, 이벤트 수, 마지막 처리 봉, pending order, active stop과 파일 해시를 기록한다.
2. 같은 원장으로 서비스를 시작해 정상 cycle 또는 안전한 `ALREADY_PROCESSED` 결과와 새 journal 기록을 확인한다.
3. `systemctl restart`를 한 번 수행하고 원장 이벤트 수가 줄지 않으며 동일 봉 중복 주문이 없고 상태 조회가 계속 성공하는지 확인한다.
4. restart 전 journal 행과 restart 후 journal 행을 모두 조회해 로그 보존을 확인한다.
5. 배포 직전 백업본을 별도 검증 디렉터리에서 `paper-status`로 읽고 원본을 덮어쓰지 않았음을 확인한다.

실제 VM reboot 시험은 SSH 연결을 끊는 운영 변경이므로 서비스 restart 시험과 분리한다. 사용자가 그 시점에 명시적으로 승인하면 재부팅 전 상태를 기록하고, 부팅 후 서비스 자동 시작·동일 원장·기존 journal 보존까지 같은 방법으로 검증한다.

## 11. Git과 운영 전환 순서

1. 이 명세 승인 후 상세 구현 계획을 작성한다.
2. 테스트 우선으로 배포 자산, unit, 런타임 lock, 문서와 검증 도구를 구현한다.
3. 코드 리뷰와 전체 테스트를 통과한 커밋만 통합 후보로 삼는다.
4. 사용자가 최종 변경과 실전 잠금 상태를 확인한 뒤 `main`에 병합·push한다.
5. 원격 `main`의 정확한 커밋을 OCI staging에 준비하고, 서버 변경 직전 검사 결과를 사용자에게 보고한다.
6. 사용자의 서버 변경 승인을 받은 뒤 최초 설치·서비스 restart·로그 보존 시험을 수행한다.
7. VM reboot 시험과 OCI 외부 백업은 각각 별도로 설명하고 승인받는다.

서버에 파일을 설치하는 것과 `main` 병합·push는 되돌림 범위가 다른 작업이므로 한 단계로 묶지 않는다. 최초 배포 전까지 서버에는 읽기 전용 점검 외의 변경을 하지 않는다.

## 12. 완료 기준과 제외 사항

다음 조건을 모두 만족해야 “OCI 모의매매 배포 준비 완료”라고 말할 수 있다.

- 원격 `main`의 검증된 커밋만 릴리스할 수 있다.
- ARM64 Python 3.12와 모든 Python 의존성이 고정되어 재현된다.
- `autobit-paper.service`가 부팅 자동 시작, 충돌 자동 재시작, 정상 SIGINT 종료를 수행한다.
- 코드 교체 뒤에도 SQLite 원장과 공개 캔들 증거가 같은 영속 경로에 남는다.
- 서비스 restart 전후 상태·중복 방지·journal 보존 시험이 통과한다.
- 기존 legacy `.env`와 홈 트리가 변하지 않고 서비스가 접근할 수 없다.
- 실전 잠금이 유지되고 실제 주문·개인 API 요청·키 사용이 0건이다.
- 새 수신 포트, 유료 Shape 확장, 추가 Block Volume 같은 과금 가능 변경이 없다.

이 기준은 전략 수익성이나 실전 운용 적합성을 뜻하지 않는다. 최소 2주와 30회 거래의 모의 관찰, 최근 7년 백테스트·walk-forward 결과 검토, 별도의 실전 활성화 설계와 승인은 계속 독립된 조건이다.

OCI Always Free 인스턴스는 낮은 사용률 조건에서 회수 대상이 될 수 있다. 이를 피하려고 인위적으로 CPU나 네트워크 부하를 만들지 않는다. 같은 Boot Volume의 백업만으로는 회수·디스크 손실을 복구할 수 없으므로 OCI Boot Volume Backup 또는 외부 보관은 최신 무료 한도와 예상 비용을 콘솔에서 확인한 뒤 사용자가 별도로 선택한다.

## 13. 공식 근거

- [OCI Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OCI Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [uv 설치](https://docs.astral.sh/uv/getting-started/installation/)
- [uv의 Python 버전 관리](https://docs.astral.sh/uv/concepts/python-versions/)
- [uv로 Python 설치](https://docs.astral.sh/uv/guides/install-python/)
