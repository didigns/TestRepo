# AISummary — 인스톨러 · 런처 · 업데이터

상용 배포용 파이프라인. 사용자 PC에 **Python 설치가 필요 없고**(비공개 런타임 번들),
설치·실행 모두 창 있는 GUI이며, 앱 시작 시 Google Drive의 `.meta`로 자동 업데이트를
확인합니다.

## 구성 요소

| 요소 | 위치 | 역할 |
|------|------|------|
| 런처 | `launcher/launcher.py` | 앱 진입점(Qt 스플래시). 업데이트 확인 → 백엔드 기동 → `/health` 대기 → Tauri 앱 실행 → 종료 시 백엔드 정리 |
| 업데이트 로직 | `launcher/aisummary_update.py` | `.meta` 다운로드/버전비교/설치파일 다운로드+SHA-256 (순수 stdlib) |
| 설치 마법사 | `installer/install.py` | Qt 설치 UI(경로/진행률/바로가기/제거 등록) |
| 제거 | `installer/uninstall.py` | Qt 확인 후 제거 |
| 런타임 빌드 | `installer/prepare-runtime.ps1` | standalone CPython + 백엔드 deps + PySide6 설치 → 슬리밍 |
| 슬리밍 | `installer/trim-runtime.ps1` | 미사용 Qt(WebEngine/QML 등) 제거로 ~500MB 절감 |
| 패키징 | `installer/build-installer.ps1` | 페이로드 zip을 .NET csc로 self-extracting `setup.exe`에 임베드 |
| .meta 생성 | `installer/publish-release.ps1` | setup.exe의 sha256/size로 `latest.meta` 작성 |
| 배포 | `installer/release.ps1` | 빌드 → Drive 폴더 복사 → latest.meta 갱신 |
| **전체 빌드** | `build-all.ps1` | 위를 한 커맨드로 |

## 실행 흐름 (설치 후)

바로가기 → `runtime\pythonw.exe launcher\launcher.py` (콘솔 없음)
→ 스플래시 → 업데이트 확인 → 백엔드(`runtime\pythonw.exe -m aisummary.api`, 창 없이) 기동
→ `127.0.0.1:8756/health` 준비 → Tauri 앱을 `AISUMMARY_NO_UPDATE=1 AISUMMARY_NO_BACKEND=1`로 실행
→ 스플래시 닫힘. 앱을 닫으면 런처가 백엔드까지 정리.

## `.meta` 포맷 (Drive id `1hsD3y7...`)

```json
{
  "version": "0.2.0",
  "pub_date": "2026-07-08T00:00:00Z",
  "notes": "사용자에게 표시되는 변경 사항",
  "mandatory": false,
  "installer": {
    "filename": "AISummary-setup.exe",
    "url": "https://drive.google.com/file/d/<INSTALLER_ID>/view?usp=drive_link",
    "size": 12345678,
    "sha256": "<64 hex>"
  }
}
```

- 앱 시작 시 이 파일을 읽어 `version`을 현재 버전과 비교.
- `installer.url` = Drive의 setup.exe 공유 링크(“링크가 있는 모든 사용자”).
- 재정의: `AISUMMARY_META_URL`(전체 URL) 또는 `AISUMMARY_META_ID`(Drive id). 끄기: `AISUMMARY_NO_UPDATE=1`.

## 전체 빌드 + 배포

```powershell
cd desktop

# 일반 릴리스 (런타임은 이미 있으면 재사용)
& .\build-all.ps1 -Version 0.2.0 -Notes '자동 업데이터 도입'

# 의존성이 바뀌었을 때 (런타임 재빌드)
& .\build-all.ps1 -Version 0.3.0 -Notes '...' -RebuildRuntime

# 최초 1회: Drive의 AISummary-setup.exe 를 "링크가 있는 모든 사용자"로 공유하고
# 그 링크를 한 번만 전달 (이후 릴리스는 latest.meta에서 자동 재사용)
& .\build-all.ps1 -Version 0.3.0 -InstallerUrl 'https://drive.google.com/file/d/<ID>/view'
```

`build-all.ps1` 단계: 버전 동기화(Cargo.toml·tauri.conf.json) → `cargo build --release`
→ 런타임 빌드/재사용 → `setup.exe` 패키징 → Drive 복사 + `latest.meta`.

Drive 폴더 기본값은 자동 탐지(`내 드라이브`/`My Drive` 아래 `LevAAISummaryVersion`).
필요 시 `-DriveDir 'G:\내 드라이브\LevAAISummaryVersion'`.

## 개별 스크립트 (수동)

```powershell
# 런타임만 (재)빌드
& .\installer\prepare-runtime.ps1                 # 다운로드+설치+슬리밍
& .\installer\trim-runtime.ps1                    # 기존 런타임만 슬리밍

# 인스톨러만 빌드
& .\installer\build-installer.ps1 -Version 0.2.0  # → dist\AISummary_0.2.0_x64-setup.exe

# .meta만 생성
& .\installer\publish-release.ps1 -Version 0.2.0 -SetupExe <setup.exe> -InstallerUrl <link>
```

## 크기 참고

번들 런타임은 슬리밍 후 약 1.0GB (핵심 ML: lancedb·pyarrow·ctranslate2·llama-cpp
·pymupdf 등). PySide6는 QtCore/QtGui/QtWidgets만 남겨 639MB→~90MB로 축소.
추가 절감(선택): 화자분리 `sherpa-onnx`+`onnxruntime`(~67MB), 파일 디코딩 `PyAV`(~66MB).

## 요구 사항 (빌드 머신)

Rust(cargo), .NET Framework(csc, 인박스), 백엔드 deps를 pip 설치할 수 있는 환경,
로컬 동기화된 Google Drive 데스크톱.
