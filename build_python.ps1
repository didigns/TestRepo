<#
  Build the Python daemons into single-file executables with PyInstaller.
  Output: pybin\levaobserver.exe , pybin\levacache.exe
  These exes embed the Python runtime + all dependencies, so target PCs
  do NOT need Python installed.

  Usage:
    powershell -ExecutionPolicy Bypass -File build_python.ps1
    powershell -ExecutionPolicy Bypass -File build_python.ps1 -SkipInstall
#>
param([switch]$SkipInstall)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "[pybuild] $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "[fail] $m"    -ForegroundColor Red; exit 1 }

# resolve script folder (never null)
$root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($root)) { try { $root = Split-Path -Parent $MyInvocation.MyCommand.Path } catch {} }
if ([string]::IsNullOrWhiteSpace($root)) { try { $root = Split-Path -Parent $MyInvocation.MyCommand.Definition } catch {} }
if ([string]::IsNullOrWhiteSpace($root)) { $root = (Get-Location).Path }
if ([string]::IsNullOrWhiteSpace($root)) { Fail "Could not resolve script folder." }
Set-Location -LiteralPath $root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "python is required." }

if (-not $SkipInstall) {
  Info "Installing runtime deps + PyInstaller..."
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt pyinstaller
}

$dist = "$root\pybin"
$work = "$root\build_py"
if (Test-Path -LiteralPath $dist) { Remove-Item -LiteralPath $dist -Recurse -Force }

# 1) folder watcher daemon
Info "Building levaobserver.exe..."
python -m PyInstaller --noconfirm --onefile --name levaobserver `
  --distpath $dist --workpath $work --specpath $work `
  --collect-submodules watchdog `
  "$root\LevAObserver\folder_observer.py"

# 2) cache worker (LevACache uses relative imports -> build via pyentry_cache.py)
Info "Building levacache.exe..."
python -m PyInstaller --noconfirm --onefile --name levacache `
  --distpath $dist --workpath $work --specpath $work `
  --paths $root --collect-submodules LevACache `
  --collect-all fitz `
  --hidden-import pypdf --hidden-import docx --hidden-import openpyxl `
  "$root\pyentry_cache.py"

if (-not (Test-Path -LiteralPath "$dist\levaobserver.exe")) { Fail "levaobserver.exe was not produced." }
if (-not (Test-Path -LiteralPath "$dist\levacache.exe"))    { Fail "levacache.exe was not produced." }
Info "Done: $dist\levaobserver.exe , $dist\levacache.exe"
