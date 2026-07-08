# AISummary — uninstaller. Removes the install dir, shortcuts, and registry.
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AISummary')
)
$ErrorActionPreference = 'SilentlyContinue'
$AppName = 'AISummary'
function Log($m) { Write-Host "[uninstall] $m" }

Get-Process -Name 'AISummary' | Stop-Process -Force
Start-Sleep -Milliseconds 400

# Shortcuts
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk") -Force
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName") -Recurse -Force

# Registry
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName" -Recurse -Force

# Files — cannot delete the running script's own dir immediately; schedule it.
if (Test-Path $InstallDir) {
    Log "삭제: $InstallDir"
    $cmd = "Start-Sleep 1; Remove-Item -LiteralPath `"$InstallDir`" -Recurse -Force"
    Start-Process powershell -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$cmd`""
}
Log "제거 완료. 사용자 데이터(~/.aisummary)는 유지됩니다."
