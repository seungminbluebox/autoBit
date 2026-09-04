param([string]$Stage = 'focused', [string[]]$TestArgs = @(), [switch]$Coverage)
$ErrorActionPreference = 'Stop'
$taskRoot = 'C:\Users\boxma\OneDrive\바탕 화면\autoBit\.worktrees\krw-btc-rebuild'
Set-Location -LiteralPath $taskRoot
$taskEvidence = Join-Path ([System.IO.Path]::GetTempPath()) ('autobit-final-followup-' + $Stage + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskEvidence | Out-Null
$testCommand = @('-B', '-m', 'pytest', '-p', 'no:cacheprovider', '--basetemp', (Join-Path $taskEvidence 'pytest'), '-q')
if ($Coverage) {
    $env:COVERAGE_FILE = Join-Path $taskEvidence '.coverage'
    $testCommand += @('--cov=autobit', '--cov-report=term-missing', '--durations=15')
}
$testCommand += $TestArgs
git rev-parse HEAD | Set-Content -LiteralPath (Join-Path $taskEvidence 'head.txt')
git diff --stat | Set-Content -LiteralPath (Join-Path $taskEvidence 'working-diff-stat.txt')
Get-Date -AsUTC -Format o | Set-Content -LiteralPath (Join-Path $taskEvidence 'started.txt')
$testCommand -join ' ' | Set-Content -LiteralPath (Join-Path $taskEvidence 'command.txt')
Write-Output $taskEvidence
& (Join-Path $taskRoot '.venv\Scripts\python.exe') @testCommand 1> (Join-Path $taskEvidence 'stdout.log') 2> (Join-Path $taskEvidence 'stderr.log')
$taskExit = $LASTEXITCODE
$taskExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'exit-code.txt')
Get-Date -AsUTC -Format o | Set-Content -LiteralPath (Join-Path $taskEvidence 'finished.txt')
Get-Content -Tail 12 -LiteralPath (Join-Path $taskEvidence 'stdout.log')
Write-Output ('Exit code: ' + $taskExit)
exit $taskExit
