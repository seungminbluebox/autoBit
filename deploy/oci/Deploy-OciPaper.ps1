[CmdletBinding(DefaultParameterSetName = "Remote")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Remote")]
    [Parameter(Mandatory = $true, ParameterSetName = "Package")]
    [ValidateSet("Prepare", "Activate")]
    [string]$Mode,

    [Parameter(Mandatory = $true, ParameterSetName = "Remote")]
    [Parameter(Mandatory = $true, ParameterSetName = "Package")]
    [string]$Commit,

    [Parameter(Mandatory = $true, ParameterSetName = "Remote")]
    [string]$HostName,

    [Parameter(Mandatory = $true, ParameterSetName = "Remote")]
    [string]$User,

    [Parameter(Mandatory = $true, ParameterSetName = "Remote")]
    [string]$IdentityFile,

    [Parameter(ParameterSetName = "Package")]
    [switch]$PackageOnly,

    [Parameter(Mandatory = $true, ParameterSetName = "Package")]
    [string]$PackageDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw $Message
}

function Require-Commit([string]$Value) {
    if ($Value -notmatch '^[0-9a-f]{40}$') {
        Fail "Commit must be a 40-character lowercase hexadecimal SHA."
    }
}

function Require-Tracked-CleanWorktree {
    $changes = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) {
        Fail "Unable to inspect the Git working tree."
    }
    if (($changes -join "`n").Trim().Length -ne 0) {
        Fail "Tracked working-tree changes must be committed before deployment."
    }
}

function Require-ExactRemoteMain([string]$ExpectedCommit) {
    $lines = @(& git ls-remote --exit-code origin refs/heads/main)
    if ($LASTEXITCODE -ne 0) {
        Fail "origin/main could not be resolved."
    }
    if ($lines.Count -ne 1 -or $lines[0] -notmatch '^([0-9a-f]{40})\s+refs/heads/main$') {
        Fail "origin/main did not return exactly one valid commit."
    }
    if ($Matches[1] -ne $ExpectedCommit) {
        Fail "Commit must exactly match the pushed origin/main commit."
    }
    & git cat-file -e ($ExpectedCommit + '^{commit}')
    if ($LASTEXITCODE -ne 0) {
        Fail "Commit is not available as a local commit object."
    }
}

function New-Bundle([string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        Fail "PackageDirectory must name a new directory."
    }
    $parent = [System.IO.Path]::GetDirectoryName($Destination)
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        Fail "PackageDirectory parent directory must exist."
    }
    [System.IO.Directory]::CreateDirectory($Destination) | Out-Null
    $archive = Join-Path -Path $Destination -ChildPath "source.tar.gz"
    $manifest = Join-Path -Path $Destination -ChildPath "bundle.env"
    & git archive --format=tar.gz --prefix=source/ ("--output=" + $archive) $Commit
    if ($LASTEXITCODE -ne 0) {
        Fail "Unable to create source archive."
    }
    $sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    $payload = "BUNDLE_VERSION=1`nCOMMIT=$Commit`nSOURCE_SHA256=$sha256`n"
    [System.IO.File]::WriteAllText($manifest, $payload, [System.Text.UTF8Encoding]::new($false))
    return [pscustomobject]@{ Archive = $archive; Manifest = $manifest }
}

function Require-RemoteArguments {
    if ([string]::IsNullOrWhiteSpace($HostName) -or
        $HostName -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$') {
        Fail "HostName must be a safe SSH host or alias."
    }
    if ([string]::IsNullOrWhiteSpace($User) -or $User -notmatch '^[a-z_][a-z0-9_-]{0,31}$') {
        Fail "User must be a safe SSH user name."
    }
    if ([string]::IsNullOrWhiteSpace($IdentityFile)) {
        Fail "IdentityFile is required for remote deployment."
    }
    $key = Get-Item -LiteralPath $IdentityFile -Force -ErrorAction Stop
    if ($key.PSIsContainer) {
        Fail "IdentityFile must be an existing regular file."
    }
}

function Require-RemoteDirectory([string]$Value) {
    if ($Value -notmatch '^/tmp/autobit-upload\.[A-Za-z0-9]{10}$') {
        Fail "Remote upload directory was invalid."
    }
}

function Invoke-PrepareRemote([string]$Archive, [string]$Manifest) {
    $sshOptions = @(
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-i", $IdentityFile
    )
    $target = "${User}@${HostName}"
    $remoteDirectory = $null
    try {
        $remoteDirectory = (& ssh @sshOptions $target "mktemp -d /tmp/autobit-upload.XXXXXXXXXX").Trim()
        if ($LASTEXITCODE -ne 0) {
            Fail "Unable to create the remote upload directory."
        }
        Require-RemoteDirectory $remoteDirectory
        & scp @sshOptions -- $Archive $Manifest ("${target}:$remoteDirectory/")
        if ($LASTEXITCODE -ne 0) {
            Fail "Unable to upload the release bundle."
        }
        $prepareCommand = @"
set -eu
cd -- $remoteDirectory
expected=`$(sed -n 's/^SOURCE_SHA256=//p' bundle.env)
[ -n "`$expected" ]
[ "`$(printf '%s\n' "`$expected" | wc -l)" -eq 1 ]
actual=`$(sha256sum -- source.tar.gz)
actual=`${actual%% *}
[ "`$actual" = "`$expected" ]
tar --extract --gzip --file source.tar.gz --to-stdout source/deploy/oci/install-release.sh > install-release.sh
tar --extract --gzip --file source.tar.gz --to-stdout source/deploy/oci/libdeploy.sh > libdeploy.sh
chmod 0700 -- install-release.sh libdeploy.sh
sudo /bin/bash -- "$remoteDirectory/install-release.sh" prepare --archive "$remoteDirectory/source.tar.gz" --manifest "$remoteDirectory/bundle.env" --commit "$Commit"
rm -f -- "$remoteDirectory/source.tar.gz" "$remoteDirectory/bundle.env" "$remoteDirectory/install-release.sh" "$remoteDirectory/libdeploy.sh"
rmdir -- "$remoteDirectory"
"@
        & ssh @sshOptions $target $prepareCommand
        if ($LASTEXITCODE -ne 0) {
            Fail "Remote prepare failed."
        }
        $remoteDirectory = $null
    }
    finally {
        if ($null -ne $remoteDirectory -and $remoteDirectory -match '^/tmp/autobit-upload\.[A-Za-z0-9]{10}$') {
            $cleanupCommand = "rm -f -- '$remoteDirectory/source.tar.gz' '$remoteDirectory/bundle.env' '$remoteDirectory/install-release.sh' '$remoteDirectory/libdeploy.sh'; rmdir -- '$remoteDirectory'"
            & ssh @sshOptions $target $cleanupCommand | Out-Null
        }
    }
}

function Invoke-ActivateRemote {
    $sshOptions = @(
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-i", $IdentityFile
    )
    $target = "${User}@${HostName}"
    $release = "/opt/autobit/releases/$Commit"
    $activateCommand = "sudo /bin/bash -- '$release/deploy/oci/install-release.sh' activate --commit '$Commit'"
    & ssh @sshOptions $target $activateCommand
    if ($LASTEXITCODE -ne 0) {
        Fail "Remote activate failed."
    }
}

Require-Commit $Commit
Require-Tracked-CleanWorktree
Require-ExactRemoteMain $Commit

if ($PackageOnly) {
    if ($Mode -ne "Prepare") {
        Fail "PackageOnly is only valid for Prepare mode."
    }
    if ([string]::IsNullOrWhiteSpace($PackageDirectory)) {
        Fail "PackageDirectory is required for PackageOnly."
    }
    New-Bundle $PackageDirectory | Out-Null
    exit 0
}

Require-RemoteArguments

if ($Mode -eq "Activate") {
    Invoke-ActivateRemote
    exit 0
}

$temporaryDirectory = $null
try {
    $temporaryDirectory = Join-Path -Path ([System.IO.Path]::GetTempPath()) -ChildPath ("autobit-oci-" + [guid]::NewGuid().ToString("N"))
    $bundle = New-Bundle $temporaryDirectory
    Invoke-PrepareRemote $bundle.Archive $bundle.Manifest
}
finally {
    if ($null -ne $temporaryDirectory -and (Test-Path -LiteralPath $temporaryDirectory -PathType Container)) {
        $tempRoot = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).Path.TrimEnd('\', '/')
        $resolvedDirectory = (Resolve-Path -LiteralPath $temporaryDirectory).Path
        if ($resolvedDirectory.StartsWith($tempRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedDirectory -Recurse -Force
        }
    }
}
