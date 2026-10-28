param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$Workspace
)
$ErrorActionPreference = 'Stop'
$sourcePath = [IO.Path]::GetFullPath($Source).TrimEnd('\')
$targetPath = [IO.Path]::GetFullPath($Target).TrimEnd('\')
$workspacePath = [IO.Path]::GetFullPath($Workspace).TrimEnd('\')
foreach ($candidate in @($sourcePath, $targetPath)) {
    if (-not $candidate.StartsWith($workspacePath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Update path is outside the authorized workspace.'
    }
}
if ($sourcePath -eq $targetPath) { throw 'Source and target must differ.' }
$exeName = 'zotero-pdf2zh-server.exe'
$targetExe = Join-Path $targetPath $exeName
$sourceExe = Join-Path $sourcePath $exeName
$manifest = Get-Content -LiteralPath (Join-Path $sourcePath 'release-manifest.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.exe_sha256) {
    throw 'Release executable hash does not match the manifest.'
}
$releaseVersion = [string]$manifest.version
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version.' }
$logDirectory = Join-Path $targetPath 'logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$statePath = Join-Path $logDirectory ('update-v' + $releaseVersion + '-result.json')
function Write-State([string]$state, [string]$message) {
    @{version=$releaseVersion; status=$state; message=$message; target=$targetPath; updated_at_utc=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
}
function Existing-App {
    @(Get-Process -Name 'zotero-pdf2zh-server' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and ([IO.Path]::GetFullPath($_.Path) -eq $targetExe) } catch { $false }
    })
}
function Config-Hashes {
    $files = @()
    $configDirectory = Join-Path $targetPath 'config'
    if (Test-Path -LiteralPath $configDirectory) {
        $files += @(Get-ChildItem -LiteralPath $configDirectory -File -Recurse)
    }
    $settingsFile = Join-Path $targetPath 'settings.json'
    if (Test-Path -LiteralPath $settingsFile) { $files += Get-Item -LiteralPath $settingsFile }
    $hashes = @{}
    foreach ($file in $files) { $hashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash }
    return $hashes
}
try {
    Write-State 'waiting_for_exit' 'The existing app is running. This helper will update it after all its windows close; no task is interrupted.'
    while ((Existing-App).Count -gt 0) { Start-Sleep -Seconds 2 }
    Write-State 'applying' 'Backing up binaries and applying the verified update. User settings and core are preserved.'
    $before = Config-Hashes
    $serverDirectory = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $sourcePath))
    $backupDirectory = Join-Path $serverDirectory '_staging'
    if (-not ([IO.Path]::GetFullPath($backupDirectory)).StartsWith($workspacePath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup path is outside the workspace.'
    }
    New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
    $backup = Join-Path $backupDirectory (('installed-before-v' + $releaseVersion + '-') + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8) + '.zip')
    $backupItems = @($targetExe, (Join-Path $targetPath '_internal')) | Where-Object { Test-Path -LiteralPath $_ }
    Compress-Archive -LiteralPath $backupItems -DestinationPath $backup -CompressionLevel Optimal
    foreach ($name in @('_internal', 'README-EXE.md', 'PERFORMANCE.md', 'release-manifest.json')) {
        $item = Join-Path $sourcePath $name
        $files = if (Test-Path -LiteralPath $item -PathType Container) { @(Get-ChildItem -LiteralPath $item -File -Recurse) } else { @(Get-Item -LiteralPath $item) }
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($sourcePath.Length + 1)
            $destination = Join-Path $targetPath $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
            if ((Test-Path -LiteralPath $destination) -and ((Get-FileHash -LiteralPath $destination).Hash -eq (Get-FileHash -LiteralPath $file.FullName).Hash)) { continue }
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        }
    }
    foreach ($obsolete in @('index.html', 'bo.mp3', '_internal\index.html', '_internal\bo.mp3')) {
        $candidate = Join-Path $targetPath $obsolete
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { Remove-Item -LiteralPath $candidate -Force }
    }
    $pendingExe = Join-Path $targetPath 'zotero-pdf2zh-server.pending'
    Copy-Item -LiteralPath $sourceExe -Destination $pendingExe -Force
    Move-Item -LiteralPath $pendingExe -Destination $targetExe -Force
    $after = Config-Hashes
    if ($before.Count -ne $after.Count) { throw 'User configuration file count changed unexpectedly.' }
    foreach ($name in $before.Keys) {
        if ($before[$name] -ne $after[$name]) { throw 'A user configuration hash changed unexpectedly.' }
    }
    if ((Get-FileHash -LiteralPath $targetExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.exe_sha256) {
        throw 'Installed executable verification failed.'
    }
    Write-State 'complete' ('Update installed. User configuration hashes are unchanged. Binary backup: ' + $backup)
} catch {
    Write-State 'error' $_.Exception.Message
    exit 1
}
