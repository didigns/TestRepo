# OwnYourPC — custom Windows installer (payload script).
#
# This runs *inside* the self-extracting setup.exe (built by
# build-installer.ps1). At that point the extraction dir holds the app payload:
#   OwnYourPC.exe          (Tauri shell)
#   frontend\              (web UI)
#   backend\               (Python FastAPI backend)
#   icons\icon.ico
#
# It installs to %LOCALAPPDATA%\Programs\OwnYourPC, sets up a Python venv for
# the backend, creates Start Menu + Desktop shortcuts, and registers an
# uninstaller in Add/Remove Programs.
#
# Standalone use (e.g. from a build tree) is also supported:
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Payload <dir>

[CmdletBinding()]
param(
    # Directory containing the app payload. Defaults to the script's own dir
    # (true when embedded in the self-extractor).
    [string]$Payload = $PSScriptRoot,
    # Install target.
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\OwnYourPC'),
    # Skip the Python venv/deps step (offline / already provisioned).
    [switch]$NoBackendSetup
)

$ErrorActionPreference = 'Stop'
$AppName = 'OwnYourPC'
$Publisher = 'OwnYourPC'
function Log($m) { Write-Host "[install] $m" }

Log "설치 시작 → $InstallDir"

# --- 1) Read version (from version.txt in payload, if present) ------------
$Version = '0.0.0'
$verFile = Join-Path $Payload 'version.txt'
if (Test-Path $verFile) { $Version = (Get-Content $verFile -Raw).Trim() }
Log "버전 $Version"

# --- 2) Stop a running instance so files can be replaced ------------------
Get-Process -Name 'OwnYourPC' -ErrorAction SilentlyContinue | ForEach-Object {
    Log "실행 중인 앱 종료 (PID $($_.Id))"
    $_ | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

# --- 3) Copy payload ------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($item in 'OwnYourPC.exe','frontend','backend','icons') {
    $src = Join-Path $Payload $item
    if (Test-Path $src) {
        Log "복사: $item"
        Copy-Item $src -Destination $InstallDir -Recurse -Force
    }
}
Set-Content -Path (Join-Path $InstallDir 'version.txt') -Value $Version -Encoding UTF8

# --- 4) Python backend venv + deps ---------------------------------------
if (-not $NoBackendSetup) {
    $backendDir = Join-Path $InstallDir 'backend'
    $req = Join-Path $backendDir 'requirements.txt'
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue) }
    if ($py -and (Test-Path $req)) {
        $venv = Join-Path $InstallDir 'runtime'
        Log "Python 가상환경 생성: $venv"
        & $py.Source -m venv $venv
        $venvPy = Join-Path $venv 'Scripts\python.exe'
        Log "백엔드 의존성 설치 (시간이 걸릴 수 있습니다)"
        & $venvPy -m pip install --upgrade pip --quiet
        & $venvPy -m pip install -r $req --quiet
    } else {
        Log "경고: Python을 찾지 못해 백엔드 의존성 설치를 건너뜁니다. 앱 최초 실행 전에 python을 설치하세요."
    }
}

# --- 5) Shortcuts ---------------------------------------------------------
$exe = Join-Path $InstallDir 'OwnYourPC.exe'
$ico = Join-Path $InstallDir 'icons\icon.ico'
if (-not (Test-Path $ico)) { $ico = $exe }
function New-Shortcut($lnkPath) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    $sc.TargetPath = $exe
    $sc.WorkingDirectory = $InstallDir
    $sc.IconLocation = $ico
    $sc.Description = 'OwnYourPC — 로컬 문서 RAG + 회의록'
    $sc.Save()
}
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
New-Shortcut (Join-Path $startMenu "$AppName.lnk")
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk")
Log "바로가기 생성 완료"

# --- 6) Register uninstaller (Add/Remove Programs) ------------------------
$uninstallPs1 = Join-Path $InstallDir 'uninstall.ps1'
$uninstallSrc = Join-Path $Payload 'uninstall.ps1'
if (Test-Path $uninstallSrc) { Copy-Item $uninstallSrc $uninstallPs1 -Force }

$regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
New-Item -Path $regKey -Force | Out-Null
$sizeKb = [int]((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
$props = @{
    DisplayName     = $AppName
    DisplayVersion  = $Version
    Publisher       = $Publisher
    DisplayIcon     = $ico
    InstallLocation = $InstallDir
    UninstallString = "powershell -ExecutionPolicy Bypass -File `"$uninstallPs1`""
    NoModify        = 1
    NoRepair        = 1
    EstimatedSize   = $sizeKb
}
foreach ($k in $props.Keys) {
    $type = if ($props[$k] -is [int]) { 'DWord' } else { 'String' }
    New-ItemProperty -Path $regKey -Name $k -Value $props[$k] -PropertyType $type -Force | Out-Null
}

Log "설치 완료: OwnYourPC v$Version"
Log "시작 메뉴 또는 바탕화면 바로가기로 실행하세요."
