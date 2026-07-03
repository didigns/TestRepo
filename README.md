# LevAAISummary

윈도우 우측 하단에 뜨는 **원형 AI 비서 HUD**입니다. 항상 화면 위에 떠 있는 투명한 오브(orb)를 클릭하면 말풍선으로 대화할 수 있고, 온디바이스 LLM(Ollama)과 연동됩니다.

## 특징

- 프레임 없는 투명 창, 항상 위(always-on-top), 작업표시줄에 표시 안 됨
- 우측 하단 자동 배치, 빈 영역은 클릭이 뒤 창으로 통과됨
- 숨쉬는 코어 + 펄스 애니메이션의 AI 오브
- 오브 클릭 → 입력창 → 위로 올라오는 말풍선(사용자/AI)
- 로컬 LLM(Ollama) 연동, 미실행 시 데모 응답으로 폴백
- 오브에 마우스를 올리면 나타나는 ✕ 버튼으로 종료

## 실행 방법

1. [Node.js](https://nodejs.org) 설치 (LTS 권장)
2. 이 폴더에서 의존성 설치:

   ```bash
   npm install
   ```

3. 실행:

   ```bash
   npm start
   ```

## 온디바이스 LLM 연결 (선택)

[Ollama](https://ollama.com)를 설치한 뒤 모델을 받아 실행하세요:

```bash
ollama run llama3.2
```

- HUD는 `http://127.0.0.1:11434` 로 요청을 보냅니다.
- 다른 모델을 쓰려면 환경변수로 지정:

  ```bash
  set LEVA_MODEL=qwen2.5      # Windows CMD
  $env:LEVA_MODEL="qwen2.5"   # PowerShell
  npm start
  ```

- Ollama가 꺼져 있으면 자동으로 데모 응답을 표시합니다.

## 실행파일(.exe)로 빌드

```bash
npm run dist
```

`dist/` 폴더에 포터블 실행파일이 생성됩니다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `main.js` | Electron 메인 프로세스 — 창 생성/배치, 클릭 통과, LLM 호출 |
| `preload.js` | 렌더러 ↔ 메인 안전 브리지 (contextBridge) |
| `index.html` | 오브 · 말풍선 · 입력창 UI 및 스타일/애니메이션 |
| `renderer.js` | 상호작용 로직 (말풍선, 입력, 클릭 통과 토글) |

## 커스터마이징 팁

- 창 크기/위치: `main.js`의 `winW`, `winH`, `margin`
- 오브 색상: `index.html`의 `#orb` 그라디언트
- 말풍선 최대 개수: `renderer.js`의 `MAX_BUBBLES`

## 폴더 관리 (LevAObserver 연동)

설정 창의 **📁 폴더 관리** 섹션에서 실시간 파일 감시를 제어합니다. 앱 시작 시 `LevAObserver/folder_observer.py --daemon` 이 자동 실행되고, NDJSON 프로토콜로 통신합니다.

- 폴더 추가/삭제, 하위폴더 포함 여부, 무시 패턴(`*.tmp` 등)을 설정
- 등록한 폴더는 저장되어 다음 실행 시 자동 복원
- 파일 생성·삭제·이동 이벤트가 HUD 오브 위 말풍선으로 표시 (modified는 잦아서 생략)
- 감시에는 Python + `watchdog` 필요: `pip install watchdog`
- Python이 인식되지 않으면 설정 창의 "Python 실행 경로"에 전체 경로 지정

설정은 `%APPDATA%/levaaisummary/leva-config.json` 에 저장됩니다.

## AI 캐싱 (LevACache + llama.cpp)

파일이 추가/수정되면 자동으로 요약·키워드를 미리 뽑아 SQLite에 캐싱합니다.

- **모델 라우팅**: pdf·이미지 → MiniCPM-V(비전), 그 외 문서 → Gemma
- **구동**: `llama-server.exe` 1개를 필요한 모델로 스왑(재기동)하며 사용
- **트리거**: 새 파일·수정 파일 자동 + 앱 시작 시 감시 폴더 백필(해시 다른 파일만)
- **캐시 항목**: 경로, 파일명, 확장자, 크기, 수정시각, content_hash, 모델, 키워드, 요약
- **저장 위치**: `%APPDATA%/levaaisummary/leva-cache.db`
- 캐싱 진행/완료가 오브 말풍선(🧠)으로 표시됨

설정 창의 **🧠 AI 캐싱** 섹션에서 `llama-server.exe`, Gemma gguf, MiniCPM-V gguf, mmproj 경로를 지정하고 최근 캐시 목록을 확인할 수 있습니다.

캐싱은 3단계 파이프라인으로 진행됩니다: ① 본문 준비(텍스트 추출/비전 OCR, OCR 결과는 DB에 저장돼 재사용) → ② 청크 임베딩(이후 의미검색·채팅 가능) → ③ 키워드·요약(Gemma). 폴더 스캔과 파일 이벤트 배치는 '단계 우선'으로 처리해 vision↔text 모델 스왑을 배치당 최대 1회로 줄입니다.

구성 요소(`LevACache/`): `stages.py`(3단계 파이프라인, 의존성 주입), `worker.py`(NDJSON 데몬 + ctx 어댑터), `config.py`(설정), `router.py`(확장자→모델), `extract.py`(텍스트/이미지 준비), `llama_manager.py`(서버 스왑), `summarize.py`(키워드·요약 프롬프트/파싱), `db.py`(SQLite 메타/키워드/OCR), `vectors.py`(청크 벡터 저장·검색).

> 참고: 캐싱에는 llama.cpp 바이너리와 gguf 모델이 필요합니다. PDF를 이미지로 렌더링하려면 `PyMuPDF`, docx/xlsx 텍스트 추출에는 `python-docx`/`openpyxl`가 있으면 좋습니다(없으면 해당 형식은 건너뜀).
