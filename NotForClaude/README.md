# OwnYourPC

**100% 로컬** 문서 RAG Q&A + 실시간 회의 전사/회의록 데스크톱 제품. 클라우드 전송 0, 프라이버시 우선.

- 지정 폴더의 문서/PDF를 자동 벡터화 → 로컬 벡터 DB(LanceDB)
- 저사양 노트북에서 도는 SLM(**Gemma 4 E4B** via Ollama)으로 RAG 질의응답 — **인용 강제 + 신뢰도 게이팅으로 할루시네이션 억제**
- 실시간 회의 전사(faster-whisper) → SLM 요약 → **PDF 회의록**
- 설치 시 하드웨어 감지 → 티어별 모델/파라미터 자동 선택

전체 설계는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 참조.

## 상태 (v0.1 — foundation)

실행 가능한 백엔드 코어입니다. 아래가 구현되어 있습니다:

| 모듈 | 파일 | 상태 |
|------|------|------|
| 하드웨어 감지 + 모델 선택 | `ownyourpc/hardware.py` | ✅ 동작 (deps 없이 실행) |
| 설정 / 티어 | `ownyourpc/config.py` | ✅ |
| LLM/임베딩 provider (Ollama) | `ownyourpc/llm.py` | ✅ |
| 인제스천 (watch·parse·chunk·embed) | `ownyourpc/ingest/` | ✅ |
| RAG 엔진 (grounding·citation·게이팅) | `ownyourpc/rag/` | ✅ (NumPy 폴백으로 테스트 가능) |
| 회의 전사/요약/PDF | `ownyourpc/meetings/` | ✅ 스캐폴드 |
| FastAPI 로컬 서비스 | `ownyourpc/api.py` | ✅ |

후속: 실시간 WebSocket 자막, 골든셋 평가 하니스, Tauri UI 셸, 인스톨러 (ARCHITECTURE.md §10 로드맵).

## 설치

기본 백엔드는 **llama-cpp-python(인프로세스 GGUF)** 입니다 — Ollama 데몬이 필요 없습니다.

```bash
# 1) Python 의존성
cd backend
pip install -r requirements.txt

# 2) GGUF 모델 다운로드 (gemma-4-E4B + nomic-embed, 하드웨어 티어에 맞게)
python -m ownyourpc.cli pull

# 3) 환경 점검 (하드웨어·백엔드·모델·생성 프로브)
python -m ownyourpc.cli doctor

# 4) 서비스 실행 — 자동 재시작 슈퍼바이저 권장 (크래시 시 자동 복구)
python run.py
#    (단순 실행: python -m ownyourpc.api)
#    크래시 로그: ~/.ownyourpc/server.log
```

> Ollama 백엔드를 선호하면 `~/.ownyourpc/config.json` 에서 `"backend": "ollama"` 로
> 바꾸고 `ollama pull gemma4:e4b && ollama pull nomic-embed-text` 를 실행하세요.

## 빠른 사용 (CLI — 주인 PC에서 실행)

모델을 받았다면 CLI로 바로 전체 파이프라인을 돌릴 수 있습니다.

```bash
cd backend

# 0) 환경 점검 (하드웨어·티어·Ollama·모델 확인)
python -m ownyourpc.cli doctor

# 1) 필요 모델 자동 설치 (gemma4:e4b + nomic-embed-text 등 티어별)
python -m ownyourpc.cli pull

# 2) 폴더 벡터화
python -m ownyourpc.cli ingest "C:\path\to\docs"

# 3) 근거 인용 질의 (없으면 '찾을 수 없습니다' 거부)
python -m ownyourpc.cli ask "계약 만료일은?"

# 4) 실측 품질 점수 (골든셋, PASS 기준 >=85)
python -m ownyourpc.cli eval

# 5) 회의록 요약 + PDF 생성 (저장된 전사에서)
python -m ownyourpc.cli meeting path\to\transcript.json

# 6) 실시간 회의: 마이크 녹음 → 라이브 자막 → 종료(Enter) → 요약 + PDF
python -m ownyourpc.cli meeting-live --title "주간 회의"
```

> 실시간 회의는 `sounddevice`(마이크)가 필요합니다: `pip install sounddevice`.
> UI/원격 클라이언트용으로는 WebSocket API도 있습니다:
> `POST /meetings/start` → `WS /meetings/{id}/stream` (라이브 자막) → `POST /meetings/{id}/stop` (요약+PDF).

### 원클릭 E2E 스모크 테스트 (실측 리뷰 게이트)

샘플 계약서를 만들어 인제스천→질의→골든셋 평가까지 실제 모델로 돌리고
RAG 점수를 출력합니다:

```bash
python scripts/smoke_test.py
```

## 데스크톱 UI

**웹 UI (지금 바로, 추가 설치 없음)** — 백엔드를 켜고 브라우저로 접속:

```bash
cd backend
python -m ownyourpc.api        # 127.0.0.1:8756 서비스 시작
```

브라우저에서 `http://127.0.0.1:8756` 열기. 문서 Q&A(근거 인용), 실시간 회의(라이브 자막→요약→PDF), 설정(하드웨어/티어) 탭이 있습니다. 세션 토큰은 페이지에 자동 주입됩니다.

**네이티브 데스크톱 앱 (Tauri)** — Rust + Node 툴체인 필요:

```bash
cd desktop
npm install
npm run dev        # 백엔드를 사이드카로 띄우고 네이티브 창에서 UI 로드
npm run build      # 인스톨러 생성
```

`desktop/src-tauri/`에 설정(tauri.conf.json)과 백엔드 사이드카 실행 코드(main.rs)가 있습니다. UI는 `frontend/index.html` 하나이며, 웹/네이티브가 동일 파일을 공유합니다.

## 빠른 사용 (API)

```bash
TOKEN=<실행 시 출력된 토큰>

# 폴더 스캔 → 벡터화
curl -X POST 127.0.0.1:8756/ingest/scan -H "X-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{"folder": "/path/to/docs"}'

# 질의 (근거 인용 포함, 없으면 '찾을 수 없습니다')
curl -X POST 127.0.0.1:8756/query -H "X-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{"question": "계약 만료일은?"}'
```

## 테스트 / 리뷰 게이트

```bash
cd backend
pytest -q            # 코어 로직 유닛 테스트 (Ollama 불필요)
pip-audit            # 의존성 취약점 스캔
```

## 보안

로컬 온리 · 127.0.0.1 바인딩 · 세션 토큰 인증 · 텔레메트리 기본 OFF. 상세는 ARCHITECTURE.md §7.
