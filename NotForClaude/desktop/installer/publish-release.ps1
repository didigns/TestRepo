# OwnYourPC — generate the .meta for a release.
#
# Computes SHA-256 + size of a built setup.exe and writes updater/latest.meta.
# Upload the setup.exe to Google Drive (share = "anyone with the link"), then
# pass its share link via -InstallerUrl (or paste it into latest.meta after).
# Finally upload latest.meta to the SAME Drive .meta file the app reads.
#
#   powershell -ExecutionPolicy Bypass -File installer\publish-release.ps1 `
#       -Version 0.2.0 `
#       -SetupExe dist\OwnYourPC_0.2.0_x64-setup.exe `
#       -InstallerUrl 'https://drive.google.com/file/d/<INSTALLER_ID>/view?usp=drive_link' `
#       -Notes '자동 업데이터 도입'

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Version,
    [Parameter(Mandatory)] [string]$SetupExe,
    [string]$InstallerUrl = 'https://drive.google.com/file/d/PUT_INSTALLER_FILE_ID_HERE/view?usp=drive_link',
    [string]$Notes = '',
    [switch]$Mandatory,
    [string]$OutMeta = (Join-Path $PSScriptRoot '..\updater\latest.meta')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $SetupExe)) { throw "setup.exe를 찾을 수 없음: $SetupExe" }

$file = Get-Item $SetupExe
$sha  = (Get-FileHash $SetupExe -Algorithm SHA256).Hash.ToLower()
$meta = [ordered]@{
    version   = $Version
    pub_date  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    notes     = $Notes
    mandatory = [bool]$Mandatory
    installer = [ordered]@{
        filename = $file.Name
        url      = $InstallerUrl
        size     = $file.Length
        sha256   = $sha
    }
}

$json = $meta | ConvertTo-Json -Depth 5
New-Item -ItemType Directory -Force -Path (Split-Path $OutMeta) | Out-Null
# UTF-8 WITHOUT BOM — the Rust updater parses this as plain UTF-8 JSON and a
# leading BOM would break serde_json.
[System.IO.File]::WriteAllText($OutMeta, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[publish] latest.meta 작성: $OutMeta" -ForegroundColor Green
Write-Host "  version : $Version"
Write-Host "  size    : $($file.Length) bytes"
Write-Host "  sha256  : $sha"
Write-Host ""
Write-Host "다음 단계:" -ForegroundColor Yellow
Write-Host "  1) $($file.Name) 를 Google Drive에 업로드 → 공유 '링크가 있는 모든 사용자'"
Write-Host "  2) 그 공유 링크를 latest.meta 의 installer.url 에 넣기 (또는 -InstallerUrl 로 재실행)"
Write-Host "  3) latest.meta 를 앱이 읽는 .meta Drive 파일에 덮어쓰기 업로드"
