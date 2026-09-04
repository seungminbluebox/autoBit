$ErrorActionPreference = 'Stop'
$taskRoot = 'C:\Users\boxma\OneDrive\바탕 화면\autoBit\.worktrees\krw-btc-rebuild'
$taskEvidence = 'C:\Users\boxma\AppData\Local\Temp\autobit-final-followup-full-85887b2b806f4647a8ca23b1dd828f1f'
Set-Location -LiteralPath $taskRoot
if ((Get-Content -LiteralPath (Join-Path $taskEvidence 'exit-code.txt')).Trim() -ne '0') {
    throw 'The sole full suite must have completed successfully before post checks.'
}
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Get-Date -AsUTC -Format o | Set-Content -LiteralPath (Join-Path $taskEvidence 'post-started.txt')
& $taskPython -X ('pycache_prefix=' + (Join-Path $taskEvidence 'compile-cache')) -m compileall -q src tests 1> (Join-Path $taskEvidence 'compile-stdout.log') 2> (Join-Path $taskEvidence 'compile-stderr.log')
$taskCompileExit = $LASTEXITCODE
$taskCompileExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'compile-exit.txt')
if ($taskCompileExit -ne 0) { throw 'Compile failed' }
$taskExpected = 'data-download,data-quality,backtest,walk-forward,paper-once,paper-run,paper-status'
& $taskPython -B -c 'import argparse; from autobit.cli import build_parser; p=build_parser(); a=next(a for a in p._actions if isinstance(a,argparse._SubParsersAction)); expected={"data-download","data-quality","backtest","walk-forward","paper-once","paper-run","paper-status"}; assert set(a.choices)==expected; print(",".join(a.choices))' 1> (Join-Path $taskEvidence 'command-contract-stdout.log') 2> (Join-Path $taskEvidence 'command-contract-stderr.log')
$taskContractExit = $LASTEXITCODE
$taskContractExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'command-contract-exit.txt')
if ($taskContractExit -ne 0) { throw 'Exact seven command contract failed' }
foreach ($taskCommand in $taskExpected.Split(',')) {
    & $taskPython -B -m autobit $taskCommand --help 1> (Join-Path $taskEvidence ('help-' + $taskCommand + '-stdout.log')) 2> (Join-Path $taskEvidence ('help-' + $taskCommand + '-stderr.log'))
    $taskHelpExit = $LASTEXITCODE
    $taskHelpExit | Set-Content -LiteralPath (Join-Path $taskEvidence ('help-' + $taskCommand + '-exit.txt'))
    if ($taskHelpExit -ne 0) { throw ('Help failed: ' + $taskCommand) }
}
& $taskPython -B -m pytest -p no:cacheprovider --basetemp (Join-Path $taskEvidence 'pytest-post-safety') -q tests/safety/test_no_live_surface.py 1> (Join-Path $taskEvidence 'safety-stdout.log') 2> (Join-Path $taskEvidence 'safety-stderr.log')
$taskSafetyExit = $LASTEXITCODE
$taskSafetyExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'safety-exit.txt')
if ($taskSafetyExit -ne 0) { throw 'Public-only safety failed' }
git diff --check 1> (Join-Path $taskEvidence 'diff-check-stdout.log') 2> (Join-Path $taskEvidence 'diff-check-stderr.log')
$taskDiffExit = $LASTEXITCODE
$taskDiffExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'diff-check-exit.txt')
if ($taskDiffExit -ne 0) { throw 'Diff check failed' }
git diff --exit-code 7f4afb8283b8504310e856b00783c5a2e30b6d65 -- src tests docs 1> (Join-Path $taskEvidence 'tested-source-diff.log') 2> (Join-Path $taskEvidence 'tested-source-diff-stderr.log')
$taskSourceExit = $LASTEXITCODE
$taskSourceExit | Set-Content -LiteralPath (Join-Path $taskEvidence 'tested-source-diff-exit.txt')
if ($taskSourceExit -ne 0) { throw 'Source/test/docs changed after freeze' }
Get-Date -AsUTC -Format o | Set-Content -LiteralPath (Join-Path $taskEvidence 'post-finished.txt')
Write-Output 'Compile, exact seven helps, public-only safety, diff and tested-source identity checks passed.'
Get-Content -Tail 3 -LiteralPath (Join-Path $taskEvidence 'safety-stdout.log')
