# OwnYourPC — 원클릭 릴리스.
#
# 1) self-extracting setup.exe 빌드
# 2) 로컬 동기화된 Google Drive 폴더에 "고정 이름"으로 복사 (덮어쓰기 → Drive 파일 ID/공유 URL 유지)
# 3) sha256/size 계산해 같은 폴더의 latest.meta 갱신
# Drive 데스크톱이 두 파일을 자동 업로드하면 그대로 배포 완료.
#
#   powershell -ExecutionPolicy Bypass -File installer\release.ps1 -Version 0.2.0 -Notes '자동 업데이터 도입'
#
# 최초 1회만: Drive 폴더의 setup.exe를 "링크가 있는 모든 사용자"로 공유하고
# 그 링크를 -InstallerUrl 로 한 번 넘겨주면, 이후 릴리스는 latest.meta에서 자동 재사용됩니다.

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Version,
    [string]$Notes = '',
    [switch]$Mandatory,
    # 로컬 동기화된 Drive 폴더. 비워두면 자동 탐지.
    [string]$DriveDir = '',
    # Drive 폴더 이름 (자동 탐지 시 이 이름을 찾음).
    [string]$FolderName = 'LevAAISummaryVersion',
    # Drive 상의 고정 파일 이름 (덮어쓰기로 ID 유지).
    [string]$InstallerName = 'OwnYourPC-setup.exe',
    [string]$MetaName = 'latest.meta',
    # 인스톨러 공유 URL. 생략하면 기존 latest.meta의 값을 재사용.
    [string]$InstallerUrl = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
function Log($m) { Write-Host "[release] $m" -ForegroundColor Cyan }

# --- Drive 폴더 결정 (명시 > 자동 탐지) ---------------------------------
function Resolve-DriveDir {
    param([string]$Explicit, [string]$Name)
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        throw "지정한 Drive 폴더가 없음: $Explicit"
    }
    # 드라이브 문자 전체를 훑으며 흔한 Google Drive 루트 이름 아래에서 $Name 을 탐색.
    $roots = @('내 드라이브', 'My Drive', '공유 드라이브', 'Shared drives', '.')
    $tried = @()
    foreach ($drv in (Get-PSDrive -PSProvider FileSystem)) {
        foreach ($r in $roots) {
            $cand = if ($r -eq '.') { Join-Path "$($drv.Root)" $Name } else { Join-Path (Join-Path "$($drv.Root)" $r) $Name }
            $tried += $cand
            if (Test-Path -LiteralPath $cand) { return $cand }
        }
    }
    Write-Host "확인한 경로:" -ForegroundColor DarkGray
    $tried | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    throw "'$Name' 폴더를 자동으로 찾지 못함. -DriveDir 로 정확한 경로를 직접 지정하세요. 예: -DriveDir 'G:\내 드라이브\LevAAISummaryVersion'"
}

$DriveDir = Resolve-DriveDir -Explicit $DriveDir -Name $FolderName
Log "Drive 폴더: $DriveDir"

# --- 1) 인스톨러 빌드 -----------------------------------------------------
Log "인스톨러 빌드 (v$Version)"
& (Join-Path $here 'build-installer.ps1') -Version $Version
$built = Join-Path $here "..\dist\OwnYourPC_${Version}_x64-setup.exe"
if (-not (Test-Path $built)) { throw "빌드 산출물을 찾을 수 없음: $built" }

# --- 2) Drive 폴더에 고정 이름으로 복사 ----------------------------------
$driveExe  = Join-Path $DriveDir $InstallerName
$driveMeta = Join-Path $DriveDir $MetaName
Log "Drive로 복사 → $driveExe (덮어쓰기)"
Copy-Item $built $driveExe -Force

# --- 3) InstallerUrl 결정 (인자 > 기존 meta 재사용) ----------------------
if (-not $InstallerUrl -and (Test-Path $driveMeta)) {
    try {
        $prev = Get-Content $driveMeta -Raw | ConvertFrom-Json
        if ($prev.installer.url -and $prev.installer.url -notmatch 'PUT_INSTALLER_FILE_ID_HERE') {
            $InstallerUrl = $prev.installer.url
            Log "기존 installer.url 재사용"
        }
    } catch { }
}
if (-not $InstallerUrl) {
    $InstallerUrl = 'https://drive.google.com/file/d/PUT_INSTALLER_FILE_ID_HERE/view?usp=drive_link'
    Write-Host "  ⚠  installer.url 미설정 — Drive에서 $InstallerName 공유 링크를 받아 -InstallerUrl 로 한 번 재실행하세요." -ForegroundColor Yellow
}

# --- 4) latest.meta 작성 (sha256/size 자동) ------------------------------
& (Join-Path $here 'publish-release.ps1') `
    -Version $Version `
    -SetupExe $driveExe `
    -InstallerUrl $InstallerUrl `
    -Notes $Notes `
    -Mandatory:$Mandatory `
    -OutMeta $driveMeta

Log "완료. Drive 데스크톱이 $InstallerName + $MetaName 동기화하면 배포 반영됩니다."
