# AISummary — one-command build + deploy.
#
# Full release pipeline:
#   1. Sync $Version into Cargo.toml + tauri.conf.json
#   2. Build the Tauri app            (cargo build --release)
#   3. Build the private Python runtime (first time, or -RebuildRuntime)
#   4. Package the self-extracting setup.exe (Qt installer + bundled runtime)
#   5. Copy to the local Google Drive folder + write latest.meta
#
# Usage (from desktop\):
#   & .\build-all.ps1 -Version 0.2.0 -Notes '자동 업데이터 도입'
#   & .\build-all.ps1 -Version 0.3.0 -Notes '...' -RebuildRuntime          # deps changed
#   & .\build-all.ps1 -Version 0.3.0 -InstallerUrl 'https://drive.google.com/file/d/<ID>/view'
#
# First release only: share the Drive setup.exe as "anyone with the link" and
# pass its link once via -InstallerUrl; later runs reuse it from latest.meta.
#
# Prereqs: Rust (cargo) + a build machine that can pip-install the backend deps.

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Version,
    [string]$Notes = "",
    [string]$InstallerUrl = "",
    [switch]$Mandatory,
    [switch]$RebuildRuntime,     # force-rebuild the Python runtime
    [switch]$SkipTauri,          # skip cargo build (reuse existing exe)
    [string]$DriveDir = "",      # override Google Drive folder autodetection
    [string]$PyVersion = "3.12"
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$installer = Join-Path $here 'installer'
$tauriDir  = Join-Path $here 'src-tauri'
$runtime   = Join-Path $here 'runtime'
$noBom = New-Object System.Text.UTF8Encoding($false)
# Colored console output can throw IndexOutOfRangeException when the host has no
# real console buffer (redirected output / some terminals). Write-Say degrades
# gracefully to plain text instead of crashing the build.
function Write-Say([string]$Message, [string]$Color = $null) {
    try { if ($Color) { Write-Host $Message -ForegroundColor $Color } else { Write-Host $Message } }
    catch { try { [Console]::WriteLine($Message) } catch {} }
}
function Step($m) { Write-Say "`n=== $m ===" 'Green' }

# --- 1) version sync (no BOM: cargo/serde_json reject a BOM) --------------
Step "버전 동기화 → $Version"
$cargo = Join-Path $tauriDir 'Cargo.toml'
$c = (Get-Content $cargo -Raw) -replace '(?m)^version\s*=\s*".*?"', "version = `"$Version`""
[System.IO.File]::WriteAllText($cargo, $c, $noBom)
$conf = Join-Path $tauriDir 'tauri.conf.json'
$j = (Get-Content $conf -Raw) -replace '"version"\s*:\s*"[^"]*"', "`"version`": `"$Version`""
[System.IO.File]::WriteAllText($conf, $j, $noBom)
Write-Host "  Cargo.toml + tauri.conf.json → $Version"

# --- 2) Tauri build -------------------------------------------------------
if (-not $SkipTauri) {
    Step "Tauri 빌드 (cargo build --release)"
    Push-Location $tauriDir
    try {
        cargo build --release
        if ($LASTEXITCODE -ne 0) { throw "cargo build 실패 (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
} else {
    Write-Say "Tauri 빌드 건너뜀 (-SkipTauri)" 'Yellow'
}

# --- 3) private Python runtime -------------------------------------------
if ($RebuildRuntime -or -not (Test-Path (Join-Path $runtime 'python.exe'))) {
    Step "Python 런타임 빌드 (Python $PyVersion + 백엔드 deps + 슬리밍)"
    & (Join-Path $installer 'prepare-runtime.ps1') -PyVersion $PyVersion
} else {
    Write-Say "런타임 재사용: $runtime  (재빌드: -RebuildRuntime)" 'Yellow'
}

# --- 4 & 5) package + deploy ---------------------------------------------
Step "인스톨러 패키징 + Drive 배포"
$relArgs = @{ Version = $Version; Notes = $Notes }
if ($Mandatory)    { $relArgs.Mandatory = $true }
if ($InstallerUrl) { $relArgs.InstallerUrl = $InstallerUrl }
if ($DriveDir)     { $relArgs.DriveDir = $DriveDir }
& (Join-Path $installer 'release.ps1') @relArgs

Write-Say "`n[OK] v$Version 빌드 및 배포 완료" 'Green'
