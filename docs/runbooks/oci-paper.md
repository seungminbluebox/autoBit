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

서버의 SSH known-host 항목은 운영자가 이미 검증한 값이어야 한다. 다음 SSH 조회는 원격 상태를 바꾸지 않는다. `/home`의 legacy tree나 `.env`는 읽거나 hash하지 않으며, 필요한 경우 별도 승인된 preflight에서 `stat` metadata만 확인한다.

```powershell
$SshOptions = @("-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-i", $IdentityFile)
ssh @SshOptions "ubuntu@$HostName" 'uname -m; cat /etc/os-release; systemctl --failed --no-pager; df -h /; systemctl status autobit-paper.service --no-pager || true'
```

기대 조건은 ARM64(`aarch64`), Ubuntu 22.04, 충분한 root filesystem 여유 공간, known-host strict match다. Docker나 Podman은 필요하지 않다. 기존 writer, service 이름 충돌, 예상하지 못한 listening port가 보이면 중단하고 조사한다.

## 2. Prepare: 서비스 변경 없는 릴리스 준비

`Prepare`는 정확한 remote `main` 커밋을 패키징해 immutable release를 만들고 ARM64 runtime, frozen dependency, import, CLI, unit, 분리된 smoke ledger를 검증한다. 서비스의 stop/start/restart, enable/disable, `current` 변경을 하지 않는다.

```powershell
& ".\deploy\oci\Deploy-OciPaper.ps1" -Mode Prepare -Commit $Commit -HostName $HostName -User ubuntu -IdentityFile $IdentityFile
```

성공 출력의 `Prepared immutable release:` 경로가 `$Commit`으로 끝나는지 확인한다. 반환된 release metadata는 다음 읽기 전용 조회로 확인한다. `.autobit-release`의 `commit`, 고정 uv/Python 버전, lock/source hash가 준비 요청과 일치해야 한다.

```powershell
ssh @SshOptions "ubuntu@$HostName" "sudo cat /opt/autobit/releases/$Commit/.autobit-release"
```

Prepare 성공은 활성화 승인이 아니다. 이 시점에 현재 서비스와 운영 원장은 바뀌지 않아야 한다.

## 3. Activate: 별도 변경 승인 뒤 실행

`server-change approval`을 별도로 받은 뒤에만 다음 명령을 실행한다. Activate는 기존 paper 서비스를 정상 종료하고, 닫힌 원장 backup bundle을 만들고, unit/journald 설정을 설치한 뒤 `current`를 원자적으로 바꾸고 서비스를 enable/start한다. journald가 재시작될 수 있다.

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

`systemctl status`는 active 상태와 단일 MainPID를, `paper-status`는 `market=KRW-BTC`, `mode=normalized-paper`와 ledger health를 보여야 한다. journal은 현재 invocation의 기록을 포함해야 한다.

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

- `/home`의 legacy `.env`는 사용하지 않는다. systemd unit에는 `EnvironmentFile`이 없고 legacy tree의 내용은 읽지 않는다.
- live는 자격증명 접근·인증 요청·주문 전에 계속 잠겨 있다. OCI 서비스와 이 runbook은 paper-only 경계를 해제하지 않는다.
- journald의 `Storage`, `SystemMaxUse`, `SystemKeepFree`, retention 설정은 host 전체에 적용되는 global limits다. 다른 unit의 로그 영향까지 검토한 별도 승인 없이 바꾸지 않는다.
- `/var/backups/autobit`의 same-volume backup은 실수와 code 전환 증거에는 유용하지만 boot volume 장애나 disk loss를 보호하지 않는다.
- OCI external backup 또는 Boot Volume backup은 quota와 비용에 영향을 줄 수 있는 별도 승인 단계다. 이 runbook은 생성하지 않는다.
- Always Free 인스턴스에는 OCI idle-reclamation risk가 있다. 회수를 피하려는 artificial load는 금지한다. 정상 workload와 상태 증거를 왜곡하지 말고, 지속성이 필요하면 OCI 정책과 적절한 자원 선택을 별도로 검토한다.
- VM reboot와 OCI external backup 승인은 Prepare/Activate/service restart 승인과 각각 독립적이다. 운영자가 대화형으로 영향 범위와 복구 경로를 확인하기 전에는 진행하지 않는다.
