# OwnYourPC — slim down the private Python runtime.
#
# The app only uses PySide6's QtCore/QtGui/QtWidgets, but pip installs the full
# Qt (WebEngine/Chromium, QML/Quick, 3D, Multimedia, Designer, translations…).
# This strips everything unused, cutting ~500MB. Safe to re-run.
#
#   powershell -ExecutionPolicy Bypass -File installer\trim-runtime.ps1
#   (prepare-runtime.ps1 also calls this automatically)

[CmdletBinding()]
param(
    [string]$Runtime = ""
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $Runtime) { $Runtime = Join-Path $here "..\runtime" }
$Runtime = [System.IO.Path]::GetFullPath($Runtime)
function Log($m) { Write-Host "[trim] $m" -ForegroundColor Cyan }

if (-not (Test-Path (Join-Path $Runtime 'python.exe'))) { throw "런타임 없음: $Runtime" }
$before = [math]::Round((Get-ChildItem $Runtime -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 0)
$exe = Join-Path $Runtime 'python.exe'
$sp  = Join-Path $Runtime 'Lib\site-packages'

# --- PySide6: keep only QtCore/QtGui/QtWidgets ----------------------------
$qt = Join-Path $sp 'PySide6'
if (Test-Path $qt) {
    Log "PySide6 슬리밍"
    $keepDll = @('Qt6Core.dll','Qt6Gui.dll','Qt6Widgets.dll','Qt6Svg.dll','Qt6Network.dll')
    $keepPyd = @('QtCore.pyd','QtGui.pyd','QtWidgets.pyd')
    $keepPlugins = @('platforms','styles','imageformats','iconengines')

    foreach ($d in @('resources','translations','qml','metatypes','include','typesystems',
                     'scripts','glue','doc','support','examples','qml')) {
        Remove-Item (Join-Path $qt $d) -Recurse -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem $qt -Filter 'Qt6*.dll' |
        Where-Object { $keepDll -notcontains $_.Name } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem $qt -File |
        Where-Object { $_.Name -like 'av*.dll' -or $_.Name -like 'sw*.dll' -or $_.Name -eq 'pyside6qml.abi3.dll' } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem $qt -Filter 'Qt*.pyd' |
        Where-Object { $keepPyd -notcontains $_.Name } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem $qt -Filter '*.exe' | Remove-Item -Force -ErrorAction SilentlyContinue
    $plugins = Join-Path $qt 'plugins'
    if (Test-Path $plugins) {
        Get-ChildItem $plugins -Directory |
            Where-Object { $keepPlugins -notcontains $_.Name } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- dev-only packages ----------------------------------------------------
Log "개발용 패키지 제거 (pytest, pip-audit)"
& $exe -m pip uninstall -y pytest pip-audit cyclonedx-python-lib 2>$null | Out-Null

# --- caches ---------------------------------------------------------------
Get-ChildItem $Runtime -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$after = [math]::Round((Get-ChildItem $Runtime -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 0)
Log "완료: ${before}MB -> ${after}MB (절감 $($before - $after)MB)"
