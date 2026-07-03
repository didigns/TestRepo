# LevACache — AI 캐싱 단계 분석 리포트

작성일: 2026-07-03 · 대상: `LevACache/` (worker.py, stages.py, db.py, vectors.py, router.py, extract.py, summarize.py, llama_manager.py, config.py) + `main.js` 연동부

---

## 1. 전체 구조

```
Electron(main.js) ──NDJSON(stdin/stdout)── levacache 워커(worker.py --daemon)
                                              │
                              ┌───────────────┴───────────────┐
                    메인 llama-server (:8080)        임베딩 llama-server (:8081)
                    Gemma ↔ MiniCPM-V 스왑            상시 기동, 스왑 없음
```

- 트리거 2종: **① 파일 이벤트**(LevAObserver → 디바운스 → `cache` 명령, 파일 단위) / **② 앱 시작 백필**(`gather` → `scan`, 폴더 단위 배치)
- 저장소: SQLite WAL (`file_cache` 메타/키워드 + `chunk_vectors` 청크 벡터)

## 2. 캐싱 단계 (실제 구현 기준: worker.py + db.py)

| stage | 이름 | 사용 모델 | 하는 일 | 이후 가능해지는 것 |
|---|---|---|---|---|
| 0 | 대기(PENDING) | — | `gather`/`register_discovered`로 목록만 선등록 | UI에 전체 목록 표시 |
| 1 | 본문 준비 | (이미지·스캔PDF만) MiniCPM-V | 텍스트 추출 또는 비전 OCR → `bodies` 메모리에 보관 | — |
| 2 | 임베딩 | 임베딩 모델(별도 서버) | 본문을 1,000자 청크로 분할·임베딩 → `chunk_vectors` 저장 | **의미검색·RAG 채팅** |
| 3 | 키워드/요약 | Gemma | 2,000자 조각 map-refine 반복 추출 → `set_cached` | 키워드 검색·목록 표시, status=CACHED |

상태 흐름: `PENDING(0) → 1 → 2 → 3 → CACHED` / 실패 시 `FAILED`(error_msg 기록).
해시(`content_hash`, SHA-256) 기반 증분: 해시 동일 + CACHED + 키워드 있음 → 스킵.

## 3. 잘 설계된 점

- **싼 것 → 비싼 것 순서**: 임베딩만 끝나면(2단계) 검색·채팅이 가능해, 무거운 3단계(Gemma)를 기다리지 않아도 됨. `vectors.py`의 "청크가 하나라도 있으면 검색됨" 구조가 이를 자연스럽게 보장.
- **모델 스왑 최소화**: `_process_stagewise`가 단계 우선(breadth-first)으로 처리 — OCR(vision)은 1단계에, Gemma(text)는 3단계에 몰려 배치당 스왑 최대 1회. `_collect_supported`가 텍스트류 먼저·이미지류 뒤로 정렬해 추가로 스왑을 줄임.
- **임베딩 서버 분리**(:8081 상시 기동): 임베딩이 메인 모델과 경쟁하지 않고, 스왑 시에도 유지됨.
- **채팅 폴백 체계**: 의미검색 실패 → 토큰검색(`db.search`) 폴백. 본문 재추출 실패 → 요약·키워드로 컨텍스트 대체. 참고 파일명 누락 시 `_with_refs`가 자동 보정.
- **견고한 파싱**: `parse_result`가 thinking 블록 제거 → 균형 JSON → 정규식 → 폴백 순으로 처리. `merge_keywords`로 조각별 키워드 누적.
- **운영 배려**: WAL(쓰는 중에도 목록 읽기 가능), UTF-8 강제(cp949 깨짐 방지), 빈 파일 조용히 스킵, 실패 사유를 note로 명시(라이브러리 미설치 등), 임베딩 실패는 비치명적으로 처리해 3단계 계속 진행.

## 4. 발견된 문제점

### 4.1 `stages.py`는 데드 코드 + 단계 정의 불일치 (중요)
`Pipeline` 클래스는 **어디서도 임포트되지 않음**. 실제 로직은 worker.py에 인라인 구현돼 있고, 두 파일의 단계 정의가 서로 다름:

| | stages.py (미사용) | worker.py + db.py (실제) |
|---|---|---|
| 1단계 | 임베딩 | 본문 준비(추출/OCR) |
| 2단계 | OCR | 임베딩 |
| 3단계 | 키워드 | 키워드 |
| 상태 | PENDING→EMBEDDED→OCR_DONE→CACHED | PENDING→CACHED/FAILED (+stage 0–3) |

`EMBEDDED`/`OCR_DONE` 상태는 db.py 어디에도 저장되지 않음. **삭제하거나 실제 구현과 동기화 필요** — 유지보수 시 혼란의 원인.

### 4.2 사용되지 않는 레거시 경로
- `embed_file()` (1단계 전용 함수): 데몬 명령에 `embed` 타입이 없어 **호출 불가**.
- `_embed_entry`, `_backfill_embeddings`, `db.set_embedding`, `db.search_semantic`, `file_cache.embedding` 컬럼: 파일 단위 임베딩(구버전)으로, 청크 임베딩(`vectors.py`)으로 대체됐으나 코드가 남아 있음.

### 4.3 빈 키워드 파일의 무한 재캐싱 가능성
`needs_caching`은 keywords가 `[]`이면 재캐싱 대상으로 판단. 그런데 3단계에서 모델이 키워드를 못 뽑아도(`keywords=[]`) `set_cached`가 그대로 저장하므로, 해당 파일은 **스캔할 때마다 매번 Gemma 재분석**됨. 재시도 횟수 제한 또는 "분석했으나 키워드 없음" 마커 필요.

### 4.4 OCR 결과가 영속되지 않음
OCR 본문은 임베딩·키워드에만 쓰이고 버려짐(청크 텍스트 500자 스니펫만 잔존). 채팅 시 `_augment`가 본문을 재추출하는데 이미지/스캔PDF는 재추출 불가 → 요약·키워드로만 답변. stages.py에는 `ocr_text` 저장 설계가 있었으나 실제 구현엔 없음. **OCR 텍스트를 DB에 저장하면 이미지 파일 RAG 품질이 크게 개선**되고, 재캐싱 시 OCR 재실행도 피할 수 있음.

### 4.5 파일 이벤트 경로는 스왑 최적화 미적용
실시간 이벤트는 파일마다 `cache_file()`(깊이 우선: OCR→임베딩→키워드)을 호출. 이미지·텍스트 파일이 번갈아 추가되면 **vision↔text 스왑이 파일마다 발생**. 디바운스가 경로별로만 걸려 있어 배치화가 안 됨 — 이벤트를 짧게 모아 `_process_stagewise`로 넘기는 마이크로 배칭 권장.

### 4.6 확장성(현재 규모에선 무해)
- 벡터 검색이 매 질의마다 `chunk_vectors` **전체를 메모리 로드** 후 브루트포스 내적. `embedMaxChunks=0`(무제한) 기본값과 겹치면 파일 수천 개 규모에서 지연 발생 가능. sqlite-vec 또는 벡터 캐싱 고려.
- `_process_stagewise`의 `bodies` dict가 배치 전체 본문을 메모리에 보관(파일당 최대 12,000자라 상한은 있음).
- `db.search`(토큰검색)도 CACHED 전 행 풀스캔.

### 4.7 소소한 것
- `cache_file`의 임베딩 이벤트는 stage 2로 emit되지만 그 직전 OCR progress는 stage 1 — UI에서 단일 파일 처리 시 1→2→3이 순간적으로 지나가 스테이지 표시 의미가 약함.
- `chunk_vectors.text`를 500자로 자르므로 스니펫 표시용으로만 유효(의도된 설계로 보임).

## 5. 권장 조치 (우선순위순)

1. `stages.py` 삭제 또는 실제 구현으로 동기화 (문서 불일치 해소)
2. OCR 본문을 `file_cache`에 영속화 → 이미지 RAG 품질 개선 + 재캐싱 비용 절감
3. 빈 키워드 재캐싱 루프 차단 (재시도 카운터 또는 NO_KEYWORDS 마커)
4. 레거시 파일 단위 임베딩 코드 제거 (`embed_file`, `search_semantic` 등)
5. 파일 이벤트 마이크로 배칭으로 스왑 감소
6. (규모 커지면) 벡터 인덱스 도입

## 6. 총평

"싼 단계부터 처리해 검색을 먼저 열어주고, 비싼 분석은 배치로 몰아 모델 스왑을 최소화한다"는 핵심 설계는 온디바이스 SLM 환경에 잘 맞고 구현도 대체로 견고함. 다만 설계 문서 역할을 하는 `stages.py`가 실제 코드와 어긋난 채 방치돼 있고, OCR 결과 미영속·빈 키워드 재캐싱 같은 실질 비용이 드는 구멍이 있어 위 1–3번은 조기 정리를 권장.
