#Requires -Version 5.1
<#
Build script for Zotero PDF2zh Server thin-exe (Windows).
Usage:
    cd APP
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
Result:
    dist\release-v<version>\zotero-pdf2zh-server\ (onedir, includes our XPI)
    ..\Release\PDF2zh-v<version>-Windows-x64.zip and -update.zip

System Python can be ANY 3.x (3.12 / 3.14 all OK): this script uses the
bundled uv to provision a dedicated Python 3.12 build env, so the frozen
exe is ALWAYS built with 3.12 (matches kernel venv + upstream target).
System Python 3.14 is left untouched. Internet access required.
#>
$ErrorActionPreference = "Stop"
$ServerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ServerDir
$env:TEMP = Join-Path $ServerDir 'cache\tmp'
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = Join-Path $ServerDir 'cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ServerDir 'core\python'
$env:UV_PYTHON_BIN_DIR = Join-Path $ServerDir '_tools'
$env:UV_MANAGED_PYTHON = '1'
$env:PIP_CACHE_DIR = Join-Path $ServerDir 'cache\pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ServerDir 'cache\pyinstaller'
$env:npm_config_cache = Join-Path $ServerDir 'cache\npm'
New-Item -ItemType Directory -Force -Path $env:TEMP,$env:UV_PYTHON_INSTALL_DIR | Out-Null
function Remove-BuildDirectory([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($ServerDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to delete a build directory outside APP.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
$UvVersion = "0.7.13"
$ToolsDir = Join-Path $ServerDir "_tools"
New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
$UvExe = Join-Path $ToolsDir "uv.exe"

Write-Host "== [1/7] ensure uv (kernel downloader + py312 provisioner) =="
if (-not (Test-Path $UvExe)) {
    $urls = @(
        "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip",
        "https://gh-proxy.com/https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip",
        "https://ghproxy.cn/https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
    )
    $zip = Join-Path $env:TEMP "uv-win.zip"
    $done = $false
    foreach ($u in $urls) {
        try {
            Write-Host "download: $u"
            Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
            $done = $true; break
        } catch { Write-Warning "failed: $u ($($_.Exception.Message))" }
    }
    if (-not $done) {
        throw 'uv download failed; place the official uv.exe in APP/_tools and rerun.'
    }
    if (-not $done) { throw "uv.exe download failed. Manually place uv.exe into APP/_tools/ and rerun." }
    if (Test-Path $zip) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $tmp = Join-Path $env:TEMP "uv-win-extract"
        Remove-BuildDirectory $tmp
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp)
        $found = Get-ChildItem $tmp -Recurse -Filter "uv.exe" | Select-Object -First 1
        if (-not $found) { throw "uv.exe not found in archive" }
        Copy-Item $found.FullName $UvExe -Force
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Remove-BuildDirectory $tmp
    }
}
& $UvExe --version

Write-Host "== [2/7] provision Python 3.12 via uv (system python untouched) =="
python --version 2>$null | ForEach-Object { Write-Host "system python: $_" }
try {
    & $UvExe python install 3.12
} catch { Write-Warning "uv python install notice: $($_.Exception.Message)" }
$Py312 = $null
try {
    $found = & $UvExe python find 3.12 2>$null | Where-Object { $_ -match "\.exe$" -or $_ -match "python" } | Select-Object -Last 1
    if ($found -and (Test-Path ($found.Trim()))) { $Py312 = $found.Trim() }
} catch { }
if (-not $Py312) {
    throw 'Project-local Python 3.12 provisioning failed; no system Python will be modified.'
}
Write-Host "build python: $Py312"
& $Py312 --version
$buildVer = & $Py312 -c "import sys; print(str(sys.version_info.major) + chr(46) + str(sys.version_info.minor))"
Write-Host "build python minor: $buildVer"
if ($buildVer -ne "3.12") {
    throw "Expected Python 3.12; got $buildVer."
} else {
    Write-Host "OK: single-version build (exe host 3.12 + kernel 3.12)."
}
try {
    & $Py312 -c 'import tkinter; print("tkinter OK", tkinter.TkVersion)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 does not provide tkinter.' }
} catch {
    throw "Build python has no tkinter (launcher UI needs it). Reinstall via: uv python install 3.12 --reinstall. Error: $($_.Exception.Message)"
}

Write-Host "== [3/7] build venv (pinned to build python) =="
$VenvPy = Join-Path $ServerDir ".build-venv\Scripts\python.exe"
$rebuild = $true
if (Test-Path $VenvPy) {
    try {
        $vv = & $VenvPy -c "import sys; print(str(sys.version_info.major) + chr(46) + str(sys.version_info.minor))"
        if ($vv.Trim() -eq $buildVer.Trim()) { $rebuild = $false; Write-Host ".build-venv reuses ($vv)." }
        else { Write-Host ".build-venv is $vv, want $buildVer -> recreate." }
    } catch { Write-Host ".build-venv broken -> recreate." }
}
if ($rebuild) {
    Remove-BuildDirectory (Join-Path $ServerDir '.build-venv')
    & $Py312 -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Build venv creation failed.' }
}
& $VenvPy -m pip install -U pip

Write-Host "== [4/7] install deps (pyinstaller>=6.15 for 3.14-era compat) =="
& $VenvPy -m pip install -r requirements-build-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
& $VenvPy -m PyInstaller --version

Write-Host "== [5/7] pyinstaller =="
# Build our plugin as part of every release. Keep the existing build entry point.
$NpmCommand = Get-Command 'npm.cmd' -ErrorAction Stop
$PluginDir = Join-Path (Split-Path -Parent $ServerDir) 'Plugin'
$PluginCache = Join-Path $ServerDir 'cache\npm'
Push-Location -LiteralPath $PluginDir
try {
    if (-not (Test-Path -LiteralPath 'node_modules')) {
        & $NpmCommand.Source ci --cache $PluginCache --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Plugin dependency installation failed.' }
    }
    & $NpmCommand.Source run build
    if ($LASTEXITCODE -ne 0) { throw 'Zotero plugin compilation failed.' }
} finally { Pop-Location }
$ReleaseVersion = (& $VenvPy -c 'from utils.version import VERSION; print(VERSION)').Trim()
if ($LASTEXITCODE -ne 0 -or $ReleaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version.' }
$ReleaseDir = Join-Path $ServerDir ('dist\release-v' + $ReleaseVersion)
$OutDir = Join-Path $ReleaseDir 'zotero-pdf2zh-server'
if ((Test-Path -LiteralPath (Join-Path $OutDir 'settings.json')) -or
    (Test-Path -LiteralPath (Join-Path $OutDir 'config\llm_apis.json')) -or
    (Test-Path -LiteralPath (Join-Path $OutDir 'core\zotero-pdf2zh-next-venv'))) {
    throw 'Release directory contains a user installation. Build into a fresh release version.'
}
$ReleaseExe = Join-Path $OutDir 'zotero-pdf2zh-server.exe'
if (Get-Process -Name 'zotero-pdf2zh-server' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $ReleaseExe }) {
    throw 'Release EXE is running. Do not overwrite an active installation.'
}
& $VenvPy -m PyInstaller --clean --noconfirm --distpath $ReleaseDir zotero-pdf2zh-server.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE compilation failed.' }
if (-not (Test-Path (Join-Path $OutDir "zotero-pdf2zh-server.exe"))) {
    throw "build output missing: $OutDir"
}

Write-Host "== [6/7] bundle Python 3.12 interpreter (offline kernel -> <exe>\core\python) =="
$StagePy = Join-Path $ServerDir "_staging\python-bundle"
New-Item -ItemType Directory -Force -Path $StagePy | Out-Null
function Get-StagedPython {
    $e = Get-ChildItem $StagePy -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $e) { return $null }
    try {
        $v = & $e.FullName -c "import sys; print(str(sys.version_info.major) + chr(46) + str(sys.version_info.minor))" 2>$null
        if ($v.Trim() -eq "3.12") { return $e }
        Write-Warning "staging python is $v, want 3.12 -> rebuild staging."
        Remove-BuildDirectory $StagePy
        New-Item -ItemType Directory -Force -Path $StagePy | Out-Null
    } catch { }
    return $null
}
$StagedExe = Get-StagedPython
if (-not $StagedExe) {
    Write-Host "provision 3.12 into staging (pinned dir, no user-profile dependency)..."
    try {
        & $UvExe python install 3.12 --install-dir $StagePy
    } catch { Write-Warning "uv python install notice: $($_.Exception.Message)" }
    $StagedExe = Get-StagedPython
}
if (-not $StagedExe) {
    Write-Host "fallback: copy from uv-managed 3.12 (uv python find)..."
    try {
        $mp = & $UvExe python find 3.12 2>$null | Where-Object { $_ -match "python" } | Select-Object -Last 1
        if ($mp) { $mp = $mp.Trim().Trim([char]34) }
        if ($mp -and (Test-Path $mp)) {
            $mv = & $mp -c "import sys; print(str(sys.version_info.major) + chr(46) + str(sys.version_info.minor))" 2>$null
            if ($mv.Trim() -eq "3.12") {
                $srcDir = Split-Path -Parent $mp
                $dest = Join-Path $StagePy (Split-Path -Leaf $srcDir)
                Write-Host "copy managed 3.12: $srcDir -> $dest"
                Remove-BuildDirectory $dest
                Copy-Item $srcDir $dest -Recurse -Force
                $StagedExe = Get-StagedPython
            } else { Write-Warning "managed python is $mv, not 3.12; skip copy." }
        } else { Write-Warning "uv python find 3.12 returned nothing usable." }
    } catch { Write-Warning "copy managed python notice: $($_.Exception.Message)" }
}
if (-not $StagedExe) { throw "Python 3.12 bundle failed (no network + no local 3.12). Connect network or install Python 3.12 from python.org, then rerun." }
& $StagedExe.FullName --version
$bundleDest = Join-Path $OutDir "core\python"
New-Item -ItemType Directory -Force -Path $bundleDest | Out-Null
foreach ($d in (Get-ChildItem $StagePy -Directory)) {
    $t = Join-Path $bundleDest $d.Name
    Remove-BuildDirectory $t
    Copy-Item $d.FullName $t -Recurse -Force
}
$pyv = & $StagedExe.FullName --version 2>&1
Set-Content -Path (Join-Path $bundleDest "BUNDLED.txt") -Value "bundled-python: $pyv`nbuilt: $(Get-Date -Format 'yyyy-MM-dd HH:mm')`nnote: kernel venv uses this offline; no GitHub download needed.`n" -Encoding UTF8
$bsize = [math]::Round(((Get-ChildItem $bundleDest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "bundled python: $pyv ($bsize MB) -> $bundleDest"

Write-Host "== [7/7] zip package =="
Copy-Item -LiteralPath 'README-EXE.md','PERFORMANCE.md','LICENSE' -Destination $OutDir -Force
& $VenvPy tools/package_release.py --release-root $OutDir
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
$ZipOut = Join-Path (Split-Path -Parent $ServerDir) ('Release\PDF2zh-v' + $ReleaseVersion + '-Windows-x64.zip')
Write-Host ""
Write-Host "DONE: $ZipOut"
Write-Host "build python : $buildVer ($Py312)"
Write-Host "kernel python: 3.12 BUNDLED in <exe>\core\python (offline; venv still pip-installs pdf2zh-next via mirrors)"
Write-Host "Install: unzip to a dedicated ENGLISH path e.g. D:\zotero-pdf2zh-server\ then run zotero-pdf2zh-server.exe"
