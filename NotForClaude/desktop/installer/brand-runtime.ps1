# AISummary — brand the bundled Python host executables.
#
# The launcher and backend run on the private runtime's pythonw.exe / python.exe,
# whose PE version resource says "Python" — so Task Manager groups them under
# "Python(N)". This rewrites their FileDescription / ProductName to "AISummary"
# (and sets the app icon) so they display and group as "AISummary". Only the
# version resource changes; filenames stay the same, so no code that references
# pythonw.exe / python.exe needs to change.
#
# Runs automatically at the end of prepare-runtime.ps1. Can also be run
# standalone against any runtime folder — CLOSE THE APP FIRST (the exes must not
# be in use):
#   powershell -ExecutionPolicy Bypass -File installer\brand-runtime.ps1 `
#       -Runtime "$env:LOCALAPPDATA\Programs\AISummary\runtime"

[CmdletBinding()]
param(
    [string]$Runtime = (Join-Path $PSScriptRoot '..\runtime'),
    [string]$Icon    = (Join-Path $PSScriptRoot '..\src-tauri\icons\icon.ico'),
    [string]$Brand   = 'AISummary'
)

$ErrorActionPreference = 'Stop'
# Colored console output can throw IndexOutOfRangeException when the host has no
# real console buffer (redirected output / some terminals). Write-Say degrades
# gracefully to plain text instead of crashing.
function Write-Say([string]$Message, [string]$Color = $null) {
    try { if ($Color) { Write-Host $Message -ForegroundColor $Color } else { Write-Host $Message } }
    catch { try { [Console]::WriteLine($Message) } catch {} }
}
function Log($m) { Write-Say "[brand] $m" 'Cyan' }

$Runtime = [System.IO.Path]::GetFullPath($Runtime)
if (-not (Test-Path $Runtime)) { throw "런타임 폴더를 찾을 수 없음: $Runtime" }

# --- Obtain rcedit (small standalone PE resource editor), cached ------------
$tools  = Join-Path $PSScriptRoot 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$rcedit = Join-Path $tools 'rcedit-x64.exe'
if (-not (Test-Path $rcedit)) {
    $url = 'https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe'
    Log "rcedit 다운로드: $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $rcedit -UseBasicParsing
}

$hasIcon = Test-Path $Icon
if (-not $hasIcon) { Log "아이콘 없음(리소스 문자열만 변경): $Icon" }

# --- Apply branding to the Python host exes ---------------------------------
$changed = 0
foreach ($name in @('pythonw.exe', 'python.exe')) {
    $exe = Join-Path $Runtime $name
    if (-not (Test-Path $exe)) { Log "건너뜀 (파일 없음): $name"; continue }

    $a = @(
        $exe,
        '--set-version-string', 'FileDescription', $Brand,
        '--set-version-string', 'ProductName',     $Brand,
        '--set-version-string', 'CompanyName',     $Brand
    )
    if ($hasIcon) { $a += @('--set-icon', $Icon) }

    Log "브랜딩: $name  ->  `"$Brand`""
    $out = & $rcedit @a 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("rcedit 실패 ($name): $out`n" +
               "앱이 실행 중이면 완전히 종료한 뒤 다시 실행하세요.")
    }
    $changed++
}

Log "완료: 실행 파일 ${changed}개 브랜딩됨  ($Runtime)"
Log "적용 확인: 작업 관리자에서 앱을 재시작하면 'Python' 대신 'AISummary'로 표시됩니다."
