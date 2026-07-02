<#
  LevAAISummary Release build script (Windows / PowerShell)

  Usage:
    powershell -ExecutionPolicy Bypass -File build.ps1               # build both installers
    powershell -ExecutionPolicy Bypass -File build.ps1 -Variant lite
    powershell -ExecutionPolicy Bypass -File build.ps1 -Variant full
    powershell -ExecutionPolicy Bypass -File build.ps1 -SkipInstall  # skip npm/pip install
    powershell -ExecutionPolicy Bypass -File build.ps1 -SkipPython   # skip python exe rebuild

  Output:
    dist\lite\LevAAISummary-<ver>-lite-Setup.exe   (no .Models)
    dist\full\LevAAISummary-<ver>-full-Setup.exe   (with .Models)
  llamacpp is included in BOTH builds.
#>
param(
  [ValidateSet("all", "lite", "full")]
  [string]$Variant = "all",
  [switch]$SkipInstall,
  [switch]$SkipPython
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "[build] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m"  -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fail] $m"  -ForegroundColor Red; exit 1 }

# ---- resolve script folder (never null) ------------------------------
$root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($root)) { try { $root = Split-Path -Parent $MyInvocation.MyCommand.Path } catch {} }
if ([string]::IsNullOrWhiteSpace($root)) { try { $root = Split-Path -Parent $MyInvocation.MyCommand.Definition } catch {} }
if ([string]::IsNullOrWhiteSpace($root)) { $root = (Get-Location).Path }
if ([string]::IsNullOrWhiteSpace($root)) { Fail "Could not resolve script folder." }
Set-Location -LiteralPath $root
Info "root folder: $root"

# ---- prerequisites ---------------------------------------------------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js is required." }
if (-not (Get-Command npm  -ErrorAction SilentlyContinue)) { Fail "npm is required." }

# llamacpp must always be present
$serverExe = "$root\llamacpp\llama-server.exe"
if (-not (Test-Path -LiteralPath $serverExe)) {
  Fail "Missing llamacpp\llama-server.exe ($serverExe). Put the llamacpp folder (binaries/DLLs) first."
}
Info "llamacpp OK: $serverExe"

# full build needs .Models (create empty if missing, with warning)
if ($Variant -ne "lite") {
  $modelsDir = "$root\.Models"
  if (-not (Test-Path -LiteralPath $modelsDir)) {
    New-Item -ItemType Directory -Path $modelsDir | Out-Null
    Warn ".Models folder was missing; created empty. Put models in it for the FULL installer to include them."
  } else {
    $cnt = (Get-ChildItem -LiteralPath $modelsDir -Recurse -File | Measure-Object).Count
    Info ".Models file count: $cnt"
    if ($cnt -eq 0) { Warn ".Models is empty; FULL installer will not include any models." }
  }
}

# ---- build Python daemon exes (PyInstaller) --------------------------
$pybin = "$root\pybin"
if (-not $SkipPython) {
  Info "Building Python daemon exes..."
  $pyArgs = @("-ExecutionPolicy", "Bypass", "-File", "$root\build_python.ps1")
  if ($SkipInstall) { $pyArgs += "-SkipInstall" }
  powershell @pyArgs
  if ($LASTEXITCODE -ne 0) { Fail "Python exe build failed." }
} else {
  Warn "Skipping Python exe build (-SkipPython)."
}
if (-not (Test-Path -LiteralPath "$pybin\levacache.exe")) {
  Fail "Missing pybin\levacache.exe. Build the Python exes first."
}
Info "Bundled Python exes OK."

# ---- npm deps --------------------------------------------------------
if (-not $SkipInstall) {
  Info "Installing npm dependencies..."
  npm install
}

# ---- build installers ------------------------------------------------
if ($Variant -eq "all" -or $Variant -eq "lite") {
  Info "Building LITE installer (no .Models, with llamacpp)..."
  npm run build:lite
}
if ($Variant -eq "all" -or $Variant -eq "full") {
  Info "Building FULL installer (with .Models and llamacpp)..."
  npm run build:full
}

# ---- results ---------------------------------------------------------
Info "Done. Artifacts:"
Get-ChildItem -Path "$root\dist" -Recurse -Filter "*Setup.exe" -ErrorAction SilentlyContinue |
  ForEach-Object { Write-Host ("  " + $_.FullName) -ForegroundColor Green }

Write-Host ""
Info "Note: Python daemons (watchdog, pypdf, PyMuPDF, python-docx, openpyxl) are bundled as pybin\*.exe, so target PCs do not need Python installed."
