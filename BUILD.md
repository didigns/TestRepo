# LevAAISummary — Release 빌드 가이드

Windows용 NSIS 설치형 인스톨러를 **두 종류**로 만듭니다.

| 빌드 | llamacpp | .Models(다운로드 모델) | 산출물 |
|---|---|---|---|
| **lite** | 포함 | 미포함(설치 후 사용자가 다운로드) | `dist/lite/LevAAISummary-<버전>-lite-Setup.exe` |
| **full** | 포함 | 포함 | `dist/full/LevAAISummary-<버전>-full-Setup.exe` |

`llamacpp`(llama-server.exe + DLL)는 두 빌드 모두 **무조건 포함**됩니다.

## 사전 준비
1. Node.js / npm 설치.
2. 프로젝트 루트에 `llamacpp/` 폴더(빌드된 llama.cpp 바이너리·DLL) 배치 — **필수**. 용량이 큰 테스트용 gguf는 빼고 실행에 필요한 파일만 두는 것을 권장합니다.
3. **full** 빌드를 하려면 루트에 `.Models/` 폴더를 두고 모델(gguf)을 넣어 둡니다. (lite 빌드는 불필요)

## 빌드
```powershell
# 두 인스톨러 모두
powershell -ExecutionPolicy Bypass -File build.ps1

# 개별
powershell -ExecutionPolicy Bypass -File build.ps1 -Variant lite
powershell -ExecutionPolicy Bypass -File build.ps1 -Variant full

# 또는 npm 스크립트로 직접
npm install
npm run build:lite     # dist/lite
npm run build:full     # dist/full
npm run build:all      # 둘 다
```

## 참고 / 주의
- **Python 번들됨:** 폴더 감시·캐싱 데몬을 PyInstaller로 `pybin\levaobserver.exe` · `pybin\levacache.exe`로 빌드해 인스톨러에 포함합니다(파이썬 런타임 + watchdog·pypdf·PyMuPDF·python-docx·openpyxl 내장). 따라서 **설치 대상 PC에 Python이 없어도 동작**합니다. 빌드 머신에는 Python + `requirements.txt` + PyInstaller가 필요합니다(`build.ps1`이 자동 설치).
- 개발 모드(`npm start`)에서는 번들 exe 대신 시스템 `python`으로 스크립트를 직접 실행합니다.
- **asar 비활성화:** 앱 내부 파일 접근 편의를 위해 `asar: false`로 빌드합니다(파일이 평문으로 설치됨).
- **사용자별 설치:** NSIS `perMachine: false` — 관리자 권한 없이 `%LOCALAPPDATA%\Programs\LevAAISummary`에 설치됩니다. 다운로드 모델(`.Models`)이 설치 폴더에 쓰이므로 쓰기 가능한 이 위치가 중요합니다.
- 런타임 경로: `llama-server.exe`는 `설치폴더\llamacpp\`, 다운로드 모델은 `설치폴더\.Models\`에서 관리됩니다.
