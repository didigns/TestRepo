# Chat 동작 분석 리포트

작성일: 2026-07-03 · 대상: LevAAISummary (HUD → Electron → LevACache 워커 → llama.cpp)

---

## 1. 전체 흐름 한눈에

```
[HUD 입력창] renderer.js send()
   │  window.leva.ask(text)
   ▼
[preload.js] ipcRenderer.invoke("ask-llm")
   ▼
[main.js] ask-llm 핸들러 → computeAnswer()
   │  sendCacheCommand("chat", {prompt}, timeout 300초)
   │  (stdin NDJSON으로 워커에 전달)
   ▼
[worker.py stdin 스레드] type=="chat" → agent.chat_q (우선 큐)
   ▼
[처리 시점 — 둘 중 먼저 오는 쪽]
   ├─ 유휴: 중량 스레드 폴링(0.2초 간격)에서 즉시
   └─ 캐싱 중: 파이프라인 checkpoint(파일 사이·키워드 조각 사이)에서
      캐싱을 잠시 멈추고 먼저 처리
   ▼
[CacheAgent.chat_stream]
   1) ensure_model("text")        ← Gemma가 아니면 서버 스왑
   2) _augment(prompt)            ← RAG: 관련 파일 검색 + 본문 주입
   3) CHAT_SYSTEM + 컨텍스트 + 질문 → llama-server SSE 스트리밍
   ▼
[응답 복귀 — 두 채널 동시]
   ├─ 조각: chat-chunk 이벤트 → main.js → HUD 스트리밍 말풍선(실시간)
   └─ 완성: result → computeAnswer 반환 → 말풍선 확정 + 파일명 링크화
      → leva-chatlog.json 기록(최대 500건)
```

---

## 2. 단계별 상세

### ① 입력 (renderer.js `send()`)

사용자 텍스트를 말풍선으로 추가하고, 오브에 `thinking` 클래스를 붙인 뒤 `window.leva.ask(text)`를 호출한다. 동시에 스트리밍 상태(`streamBubble`, `streamRaw`)를 초기화하고 타이핑 인디케이터를 띄운다.

### ② 메인 프로세스 (main.js `computeAnswer`)

`sendCacheCommand("chat", { prompt }, { timeout: 300000 })`로 워커에 위임한다. llama-server를 직접 호출하지 않고 워커를 거치는 이유는 **포트/모델 관리 주체를 워커 하나로 통일**하기 위함(스왑 충돌 방지). 오류 시 사용자 친화 메시지로 변환한다:

- `textModel/gguf/경로` 관련 오류 → "대화 모델이 아직 없어요…" 안내
- 그 외 → "llama.cpp 응답 오류: …"

### ③ 우선 큐 진입 (worker.py)

stdin 스레드는 `chat`을 중량 큐(heavy_q)가 아닌 **`agent.chat_q`(우선 큐)** 에 넣는다. 처리 시점은 두 가지:

| 워커 상태 | 처리 지점 | 최대 대기 |
|---|---|---|
| 유휴 | 중량 스레드 폴링 루프 | ≤ 0.2초 |
| 캐싱(스캔) 중 | 파일 경계 checkpoint 또는 키워드 조각 경계 | 현재 파일의 한 단계·한 조각 처리 시간 |

즉 **대화가 캐싱보다 항상 우선**하며, 캐싱은 chat 응답 후 이어서 재개된다. 여러 chat이 몰리면 온 순서(FIFO)대로 모두 비운 뒤 캐싱으로 돌아간다.

### ④ 모델 준비 (`ensure_model("text")`)

현재 로드된 모델이 text(Gemma)면 그대로 사용(재기동 없음). vision(MiniCPM-V)이 로드된 상태(1단계 OCR 중)에 chat이 오면 **메인 서버를 text로 스왑**하고, 다음 OCR 때 다시 vision으로 돌아간다 — chat 우선의 대가로 스왑 2회 비용이 든다. 3단계(키워드) 중에는 이미 text가 로드돼 있어 스왑 비용이 없다. 임베딩 서버는 별도 포트에 상시 기동이라 스왑과 무관하다.

### ⑤ RAG 검색 (`_augment` → `_retrieve`)

질문과 관련된 로컬 파일을 찾아 프롬프트에 주입한다. 상위 **2개 파일**만 사용(프리필/TTFT 절감).

1. **의미검색(우선)**: `embedModel` 설정 시 질문을 임베딩 → `chunk_vectors` 전체와 코사인 유사도(numpy 행렬곱, 실패 시 순수 파이썬 폴백) → 파일 단위로 최고 청크 점수 집계 → `embedMinScore`(기본 0.25) 이상만. 2단계(임베딩)까지만 끝난 파일도 검색된다.
2. **토큰검색(폴백)**: 임베딩 미설정/실패 시 파일명·키워드·요약에 대한 토큰 매칭 점수로 랭킹(`status='CACHED'`만 대상 — 즉 3단계 완료 파일만).

### ⑥ 컨텍스트 구성 (`_augment` → `_read_body`)

검색된 파일의 **실제 본문**을 총 7,000자 예산 안에서 주입한다:

- 텍스트 파일: 디스크에서 재추출(최신 내용 반영)
- 이미지/스캔 PDF: 캐싱 때 저장한 OCR 본문(`ocr_text` 컬럼) 사용
- 본문을 못 읽으면(삭제 등): 요약 + 키워드로 대체

최종 프롬프트 = `CHAT_SYSTEM`(고정 지시문) + "아래는 사용자의 로컬 파일 내용입니다…" + 파일 블록들 + 질문. `CHAT_SYSTEM`이 매번 동일한 프리픽스라 llama-server가 **KV 캐시를 재사용해 첫 토큰 지연(TTFT)을 줄인다**. Gemma가 system 역할을 지원하지 않아 지시문을 user 프롬프트 앞에 이어 붙이는 방식이다.

### ⑦ 생성 (llama_manager `chat_stream`)

`POST /v1/chat/completions` (OpenAI 호환), `stream: true`, `max_tokens 2048`, `temperature 0.3`, `enable_thinking: false`(사고 모드 꺼서 토큰 낭비 방지). SSE 조각이 올 때마다 `on_delta(delta)` 콜백이 호출된다.

### ⑧ 스트리밍 표시 (실시간 채널)

각 조각은 `{"type":"chat-chunk","id","delta"}`로 emit → main.js `handleCacheMessage`가 HUD 창으로 중계 → renderer `onChatChunk`가 말풍선에 누적 표시(`sanitizeMd`로 마크다운 잔재 정리). 생성 중 말풍선은 자동 소멸하지 않는다.

### ⑨ 확정·후처리

- 워커: `_with_refs`가 답변에 `[파일명]` 표기가 빠졌으면 끝에 "참고 파일: […]"을 자동 보강 → `result`로 완성 답변 반환.
- renderer: 완성 답변으로 말풍선을 확정하고, `buildFileIndex()`(캐시 목록)로 **답변 속 파일명을 클릭 가능한 링크**(뷰어 열기)로 변환. 스트림이 전혀 없었으면(폴백) 한 번에 표시.
- main.js: 질문/답변을 `leva-chatlog.json`에 기록(최대 500건 유지), 대시보드에 `chat-logged` 알림.

---

## 3. 특성과 제약

**동시성 1.** chat은 순차 처리다(단일 llama-server). 연속 질문은 큐에 쌓여 순서대로 응답된다.

**캐싱 중 대기 지연의 상한.** checkpoint는 파일·조각 경계에만 있으므로, 최악 대기는 "현재 진행 중인 작업 하나"의 시간이다 — 큰 이미지 OCR(1단계)이나 긴 조각 하나(3단계)가 진행 중이면 그만큼 기다린다. 조각(기본 2,000자) 단위라 보통 수 초 내.

**vision 로드 중 chat 비용.** 1단계 OCR 도중 chat이 오면 text 스왑 + 응답 + vision 복귀로 스왑 2회. 모델 로드가 느린 환경에서는 체감 지연이 생길 수 있다.

**검색 품질의 단계 의존성.** 의미검색은 2단계(임베딩)만 끝나면 동작하지만, 임베딩 미설정 시의 토큰검색은 3단계 완료(CACHED) 파일만 대상이다 — 재캐싱 직후에는 참조 가능한 파일이 적을 수 있다.

**타임아웃 이중 구조.** main.js 300초(IPC) vs llama HTTP 300초. 캐싱 중 checkpoint 대기 시간은 IPC 300초 안에 포함되므로, 극단적으로 느린 파일 처리 중엔 이론상 타임아웃 여지가 있다.

---

## 4. 개선 후보 (선택)

1. **chat 대기 중 표시**: 캐싱 조각 처리 때문에 대기 중일 때 HUD에 "파일 분석 마무리 중…" 같은 상태를 보내면 무응답처럼 보이는 구간이 사라진다.
2. **스왑 최소화 옵션**: 1단계(OCR) 진행 중 chat이 오면 남은 OCR 파일 수가 적을 때 스왑을 미루는 휴리스틱.
3. **대화 이력 컨텍스트**: 현재는 매 질문이 독립적(이전 대화 미주입). `leva-chatlog.json` 최근 N턴을 프롬프트에 포함하면 맥락 있는 대화가 가능(TTFT 비용과 트레이드오프).
4. **검색 상위 k 동적화**: 질문 길이/유사도 분포에 따라 1~3개로 조절해 프리필 시간 절약.
