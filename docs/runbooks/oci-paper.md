# OCI 정규화 모의매매 운영 Runbook

이 문서는 로컬 Windows 수동 실행과 OCI의 `autobit-paper.service` 운영을 분리한다. 로컬에서 직접 `paper-run`을 실행하고 종료·백업하는 절차는 [정규화 모의매매 운영 Runbook](../paper-trading-runbook.md)을 따른다. 아래 명령은 이미 검토된 정확한 remote `main` 커밋을 OCI의 paper-only 서비스로 준비·활성화·점검할 때만 사용한다. normalized-paper 초기 자산 100은 실제 KRW가 아니며 live 경계는 계속 잠겨 있다.

## 1. 읽기 전용 사전 점검

저장소 루트의 PowerShell에서 작업 트리, 선택한 커밋, remote `main`, PowerShell 7, SSH 도구, identity 파일 metadata를 확인한다. 호스트나 key 경로를 문서·스크립트·명령 기록에 붙여 넣지 않는다. `Read-Host`는 값을 PowerShell 변수에만 넣으므로 다음 명령 호출의 history에는 실제 값이 남지 않는다.

```powershell
$Commit = Read-Host "Reviewed 40-character remote main commit"
$HostName = Read-Host "OCI SSH host or alias"
$IdentityFile = Read-Host "Path to the operator-local SSH identity file"
git status --short
git ls-remote --exit-code origin refs/heads/main
git cat-file -e "$Commit^{commit}"
Get-Command pwsh, ssh, scp
Get-Item -LiteralPath $IdentityFile | Select-Object FullName, Length, LastWriteTimeUtc
```

서버의 SSH known-host 항목은 운영자가 이미 검증한 값이어야 한다. 다음 SSH 조회는 원격 상태를 바꾸지 않는다. 일반 배포와 preflight는 `/home`의 legacy tree나 `.env`를 읽거나 hash하지 않으며, 필요한 경우 별도 승인된 preflight에서 `stat` metadata만 확인한다. 아래 2.1의 명시적으로 승인된 Telegram 1회 migration만 이 규칙의 좁은 예외다.

```powershell
$SshOptions = @("-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-i", $IdentityFile)
ssh @SshOptions "ubuntu@$HostName" 'uname -m; cat /etc/os-release; systemctl --failed --no-pager; df -h /; systemctl status autobit-paper.service --no-pager || true'
```

기대 조건은 ARM64(`aarch64`), Ubuntu 22.04, 충분한 root filesystem 여유 공간, known-host strict match다. Docker나 Podman은 필요하지 않다. 기존 writer, service 이름 충돌, 예상하지 못한 listening port가 보이면 중단하고 조사한다.

## 2. Prepare: 서비스 변경 없는 릴리스 준비

`Prepare`는 정확한 remote `main` 커밋을 패키징해 immutable release를 만들고 ARM64 runtime, frozen dependency, import, CLI, unit, 분리된 smoke ledger를 검증한다. 서비스의 stop/start/restart, enable/disable, `current` 변경을 하지 않는다.

런타임 도구는 먼저 트리 경계와 archive hash, 소유권, 버전을 검증하고, 그 뒤 서비스 사용자에게 필요한 읽기·디렉터리 통과 권한만 부여한다. 권한 정규화는 경계 전체를 다시 확인한 다음 열린 파일 디스크립터에만 적용하며 group/other 쓰기는 허용하지 않는다. 마지막으로 같은 검증을 다시 실행한다. 같은 버전의 기존 도구도 `검증 → 정규화 → 재검증` 순서를 따르므로 중간 실패 뒤 재실행할 수 있다. 도구 트리 아래에 별도 mount가 있거나 외부 hard link가 있으면 아무 권한도 바꾸기 전에 실패한다.

```powershell
& ".\deploy\oci\Deploy-OciPaper.ps1" -Mode Prepare -Commit $Commit -HostName $HostName -User ubuntu -IdentityFile $IdentityFile
```

성공 출력의 `Prepared immutable release:` 경로가 `$Commit`으로 끝나는지 확인한다. 반환된 release metadata는 다음 읽기 전용 조회로 확인한다. `.autobit-release`의 `commit`, 고정 uv/Python 버전, lock/source hash가 준비 요청과 일치해야 한다.

```powershell
ssh @SshOptions "ubuntu@$HostName" "sudo cat /opt/autobit/releases/$Commit/.autobit-release"
```

Prepare 성공은 활성화 승인이 아니다. 이 시점에 현재 서비스와 운영 원장은 바뀌지 않아야 한다.

Prepare가 실패하면 출력된 `/opt/autobit/releases/.staging-<40자리 commit>-<pid>`와 `/tmp/autobit-prepare.<8자리 suffix>`를 진단 증거로 보존한다. 도구 게시 전 실패라면 `/opt/autobit/tools/<kind>/<version>.staging-<pid>`도 출력될 수 있다. 출력된 경로가 이미 최종 도구 경로로 게시되어 존재하지 않을 수도 있으므로 실제 존재 여부를 읽기 전용으로 확인한다. 경로를 추측해 삭제하거나 같은 명령을 반복하지 말고, 실패 지점과 권한·소유권을 읽기 전용으로 조사한다. 수정된 새 remote `main` 커밋으로 다시 Prepare하기 전에는 변경 범위를 다시 보고하고 승인을 받으며, 보존된 실패 증거의 삭제도 정확한 경로를 확인한 뒤 별도로 승인받는다.

### 2.1 승인된 기존 Telegram 값의 1회 분리

Telegram 연결을 사용자가 명시적으로 승인한 경우에만 준비된 정확한 릴리스의 migration 도구를 한 번 실행한다. 이 도구는 legacy `.env`를 shell로 source하거나 실행하지 않는다. `TELEGRAM_TOKEN`과 `TELEGRAM_CHAT_ID`만 읽어 형식을 검증하고 이름을 `AUTOBIT_TELEGRAM_TOKEN`과 `AUTOBIT_TELEGRAM_CHAT_ID`로 바꿔 `/etc/autobit/paper-notify.env`에 원자적으로 기록한다. `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`와 그 밖의 값은 복사하지 않는다. 비밀값은 stdout, stderr, 명령줄이나 journal에 출력하지 않는다.

```powershell
ssh @SshOptions "ubuntu@$HostName" "sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 /opt/autobit/releases/$Commit/deploy/oci/telegram_credentials.py"
ssh @SshOptions "ubuntu@$HostName" "sudo stat -c '%n|%F|%a|%U:%G|%s' /etc/autobit /etc/autobit/paper-notify.env"
```

성공 조건은 디렉터리 `/etc/autobit`가 `root:root` 모드 `0700`, 파일이 `root:root` 모드 `0600`인 것이다. 파일 내용은 `cat`, hash, shell source로 확인하지 않는다. 도구는 기존 목적 파일을 덮어쓰지 않으므로 이미 존재하면 중단하고 별도 회전 절차를 설계한다. 원본 legacy `.env`는 읽기 전후 metadata가 같아야 한다.

## 3. Activate: 별도 변경 승인 뒤 실행

`server-change approval`을 별도로 받은 뒤에만 다음 명령을 실행한다. Telegram-enabled 릴리스의 Activate는 서비스 정지 전에 `/etc/autobit/paper-notify.env`가 root 소유 모드 `0600`이며 정확히 두 Telegram 변수만 포함하는지 검사한다. 검증 뒤 기존 paper 서비스를 정상 종료하고, 닫힌 원장 backup bundle을 만들고, unit/journald 설정을 설치한 뒤 `current`를 원자적으로 바꾸고 서비스를 enable/start한다. journald가 재시작될 수 있다.

```powershell
& ".\deploy\oci\Deploy-OciPaper.ps1" -Mode Activate -Commit $Commit -HostName $HostName -User ubuntu -IdentityFile $IdentityFile
```

출력의 활성 release와 `Ledger backup evidence:` 경로를 기록한다. 기존 원장이 있었다면 backup 디렉터리는 다음을 포함한다.

- `bundle/paper.sqlite3`와 activation 시 존재했던 `bundle/paper.sqlite3-wal`; `-shm`은 durable backup이 아니므로 포함하지 않는다.
- `SHA256SUMS`, `release-before.txt`, `created-at-utc.txt`.
- `verification/paper.sqlite3`와 `verification/paper-status.json`; restore bundle 자체와 분리한 검증본이다.

최초 설치라면 `ledger-state.txt`가 `NO_EXISTING_LEDGER`를 기록한다. 활성화 뒤 검증 실패 시 rollback은 `current`와 service/config만 이전 release로 되돌리는 **code-only** 동작이다. `/var/lib/autobit`에 backup ledger를 복원하지 않는다. 복원이 필요하면 서비스를 중지한 별도 승인 절차에서 backup bundle을 검증하고 진행한다.

## 4. 상태와 로그 조회

다음 명령은 service와 원장을 변경하지 않는다. `paper-status`는 활성 SQLite 원장을 read-only로 조회한다.

```powershell
ssh @SshOptions "ubuntu@$HostName" 'sudo systemctl status autobit-paper.service --no-pager'
ssh @SshOptions "ubuntu@$HostName" 'sudo -u autobit env -i PATH=/usr/bin:/bin HOME=/var/lib/autobit XDG_CACHE_HOME=/var/lib/autobit/.cache PYTHONDONTWRITEBYTECODE=1 /opt/autobit/current/.venv/bin/python -m autobit.cli paper-status --db /var/lib/autobit/paper/paper.sqlite3'
ssh @SshOptions "ubuntu@$HostName" 'sudo journalctl -u autobit-paper.service --no-pager'
```

`systemctl status`는 active 상태와 단일 MainPID를, `paper-status`는 `market=KRW-BTC`, `mode=normalized-paper`와 ledger health를 보여야 한다. 실행 인수에는 Telegram 환경변수의 **이름**만 있고 값은 없어야 한다. journal은 현재 invocation의 기록을 포함해야 하며 알림 실패도 비밀값이나 예외 원문 없이 안전 코드로만 남아야 한다.

## 5. Inspect와 통제된 service restart 증거

`inspect`는 deploy lock을 얻은 뒤 서비스를 중지·시작·재시작하거나 원장을 복사·hash하지 않는다. 현재 service properties, 정확한 process command, `paper-status`, read-only SQLite probe, journal cursor, boot ID, release target, UTC 시각만 새 root-only evidence 디렉터리에 남긴다.

```powershell
ssh @SshOptions "ubuntu@$HostName" 'sudo bash /opt/autobit/current/deploy/oci/verify-service.sh inspect'
```

출력 경로 형식은 `/var/backups/autobit/verification/<UTC timestamp>-<commit>/`이다. root 전용 디렉터리이므로 내용을 조회하거나 반출하는 일도 운영 권한과 증거 취급 정책을 따른다.

`restart`는 service-restart 승인을 명시적으로 받은 경우에만 실행한다. 먼저 같은 before 증거를 기록하고 `autobit-paper.service`를 정확히 한 번 restart한 뒤 최대 180초 동안 active/MainPID/정확한 paper command/`paper-status`/현재 invocation journal 조건을 확인한다. 그 뒤 after 증거를 기록해 event count, event sequence, 마지막 확정 candle이 감소하지 않았는지 비교한다. candle이 진행하지 않았다면 cash, BTC 수량, position, active stop, pending orders, health stage가 정확히 같아야 한다. 저장한 이전 journal entry가 여전히 읽히고 cursor 뒤에 새 entry가 있어야 성공한다.

```powershell
ssh @SshOptions "ubuntu@$HostName" 'sudo bash /opt/autobit/current/deploy/oci/verify-service.sh restart'
```

이 명령은 VM reboot가 아니다. VM reboot는 SSH를 끊고 전체 host를 중단하는 별도 승인 단계이며 여기서 묶거나 암시하거나 실행하지 않는다.

## 6. 보안·보존·OCI 경계

- systemd는 `/home`의 legacy `.env`를 직접 사용하지 않는다. 승인된 1회 migration만 그 파일에서 두 Telegram 값을 분리하며, 이후 서비스는 `/etc/autobit/paper-notify.env`만 `EnvironmentFile`로 읽는다. legacy 원본은 수정하지 않고 `ProtectHome=true`를 유지한다.
- `/etc/autobit/paper-notify.env`에는 `AUTOBIT_TELEGRAM_TOKEN`과 `AUTOBIT_TELEGRAM_CHAT_ID`만 허용한다. 파일은 `root:root` 모드 `0600`이며 Upbit 키를 포함하면 Activate 전에 실패한다.
- live는 자격증명 접근·인증 요청·주문 전에 계속 잠겨 있다. OCI 서비스와 이 runbook은 paper-only 경계를 해제하지 않는다.
- journald의 `Storage`, `SystemMaxUse`, `SystemKeepFree`, retention 설정은 host 전체에 적용되는 global limits다. 다른 unit의 로그 영향까지 검토한 별도 승인 없이 바꾸지 않는다.
- `/var/backups/autobit`의 same-volume backup은 실수와 code 전환 증거에는 유용하지만 boot volume 장애나 disk loss를 보호하지 않는다.
- OCI external backup 또는 Boot Volume backup은 quota와 비용에 영향을 줄 수 있는 별도 승인 단계다. 이 runbook은 생성하지 않는다.
- Always Free 인스턴스에는 OCI idle-reclamation risk가 있다. 회수를 피하려는 artificial load는 금지한다. 정상 workload와 상태 증거를 왜곡하지 말고, 지속성이 필요하면 OCI 정책과 적절한 자원 선택을 별도로 검토한다.
- VM reboot와 OCI external backup 승인은 Prepare/Activate/service restart 승인과 각각 독립적이다. 운영자가 대화형으로 영향 범위와 복구 경로를 확인하기 전에는 진행하지 않는다.
