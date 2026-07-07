# OwnYourPC — build the private Python runtime that ships inside the app.
#
# Downloads a relocatable "python-build-standalone" CPython (has pip + venv +
# ssl, fully self-contained), extracts it, and pip-installs the backend
# dependencies + PySide6 into it. The resulting folder is bundled by
# build-installer.ps1 so end users never need Python.
#
#   powershell -ExecutionPolicy Bypass -File installer\prepare-runtime.ps1
#
# Output: desktop\runtime\  (python.exe, pythonw.exe, Lib\site-packages\...)
#
# Notes:
#  - Some backend wheels (llama-cpp-python, faster-whisper, sherpa-onnx) are
#    large / occasionally build from source. Ensure the same toolchain you use
#    to run the backend today is available on this build machine.
#  - Re-run only when Python version or requirements change.

[CmdletBinding()]
param(
    [string]$PyVersion = "3.12",
    [string]$Requirements = "",
    [string]$OutDir = "",
    # Optional explicit asset URL; otherwise the latest release is queried.
    [string]$PythonUrl = ""
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is empty in the param block when launched via
# `powershell -File <relative>.ps1`, so resolve the script dir here instead.
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $Requirements) { $Requirements = Join-Path $here "..\..\backend\requirements.txt" }
if (-not $OutDir) { $OutDir = Join-Path $here "..\runtime" }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Log($m) { Write-Host "[runtime] $m" -ForegroundColor Cyan }

$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$work = Join-Path $env:TEMP ("oypc_rt_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null

try {
    # --- 1) Resolve the download URL --------------------------------------
    if (-not $PythonUrl) {
        Log "최신 python-build-standalone 릴리스 조회"
        $api = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
        $rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "OwnYourPC" }
        $pat = "cpython-$([regex]::Escape($PyVersion))\..*-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
        $asset = $rel.assets |
            Where-Object { $_.name -match $pat -and $_.name -notmatch "install_only_stripped" } |
            Sort-Object name -Descending | Select-Object -First 1
        if (-not $asset) {
            throw "Python $PyVersion install_only 자산을 찾지 못함. -PythonUrl 로 직접 지정하세요."
        }
        $PythonUrl = $asset.browser_download_url
    }
    Log "다운로드: $PythonUrl"
    $tgz = Join-Path $work "python.tar.gz"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $tgz -UseBasicParsing

    # --- 2) Extract (install_only lands in a 'python' folder) -------------
    Log "압축 해제"
    tar -xzf $tgz -C $work
    $py = Join-Path $work "python"
    if (-not (Test-Path (Join-Path $py "python.exe"))) {
        throw "예상한 python\python.exe 가 없음. 아카이브 구조를 확인하세요."
    }

    # --- 3) Move into place -----------------------------------------------
    if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
    Move-Item $py $OutDir
    $exe = Join-Path $OutDir "python.exe"
    Log "런타임: $OutDir"
    & $exe --version

    # --- 4) Install dependencies ------------------------------------------
    Log "pip 업그레이드"
    & $exe -m pip install --upgrade pip --disable-pip-version-check
    if (-not (Test-Path $Requirements)) { throw "requirements.txt 없음: $Requirements" }
    Log "백엔드 의존성 설치 (시간이 걸립니다)"
    & $exe -m pip install -r $Requirements --disable-pip-version-check
    Log "PySide6 설치 (런처/인스톨러 GUI)"
    & $exe -m pip install "PySide6>=6.6" --disable-pip-version-check

    # --- 5) Slim the runtime (strip unused Qt, dev deps, caches) -----------
    & (Join-Path $here 'trim-runtime.ps1') -Runtime $OutDir

    $size = [math]::Round((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 0)
    Log "완료: 런타임 크기 약 ${size} MB"
}
finally {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
