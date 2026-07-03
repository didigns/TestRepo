# AISummary 앱(Electron) — 코드 분석 리포트

작성일: 2026-07-03 · 대상: `main.js`(1,286줄), `preload.js`(65줄), `renderer.js`(321줄), `index.html`, `settings.html`(827줄), `dashboard.html`(468줄), `viewer.html`, `setup.html`

---

## 1. 전체 구조

```
main.js (메인 프로세스, 1,286줄 단일 파일)
 ├─ 설정 저장/로드 (leva-config.json)          ├─ 채팅 로그 저장
 ├─ Observer 데몬 관리 (NDJSON)                ├─ Cache 워커 관리 (NDJSON)
 ├─ 창 5종 (HUD·설정·대시보드·뷰어·설치)        ├─ 파일 읽기 IPC (txt/docx/xlsx 파서 내장)
 ├─ HF 모델 다운로드                           └─ llama.cpp 자동 설치
preload.js — contextBridge로 window.leva API ~45개 노출
HUD(index.html + renderer.js) / 설정·대시보드·뷰어·설치(HTML 인라인 스크립트)
```

보안 기본기는 좋음: 모든 창이 `contextIsolation: true`, `nodeIntegration: false`, preload 브리지 경유. 렌더러 대부분이 `textContent`/`escapeHtml`을 사용.

## 2. 심각도 높음

### 2.1 XSS — 키워드 문자열의 innerHTML 삽입 (dashboard.html:329)
```js
head.innerHTML = "'" + selectedKw + "' 포함 파일 <span ...>(" + files.length + ")</span>";
```
`selectedKw`는 **LLM이 생성한 키워드**에서 옴 — 모델 출력은 신뢰 불가 입력이며, 파일 내용에 따라 `<img onerror=...>` 같은 문자열이 키워드로 들어올 수 있음. `textContent` + `appendChild`로 교체 필요.

### 2.2 XSS — iframe/img src 미검증 (dashboard.html:382, viewer.html:118)
```js
vc.innerHTML = '<iframe src="' + info.fileUrl + '"></iframe>';
```
`fileUrl`은 main의 `pathToFileURL()` 산출물이라 현재는 안전하지만 방어가 없음. `file:` 프로토콜 검증 + 속성 이스케이프(파일명에 `"` 포함 가능) 추가 권장.

### 2.3 캐시 초기화가 chunk_vectors를 안 지움 (기능 버그)
`cache-clear-db`(main.js:778)는 node:sqlite로 `DELETE FROM file_cache`만 실행하고, 워커 폴백(`dbmod.clear_all`)도 동일. **청크 벡터 테이블(chunk_vectors)은 남는다** → 초기화 후에도 옛 파일이 의미검색에 걸릴 수 있음(감시 폴더에서 제외된 파일이면 재캐싱으로도 정리 안 됨). 파이썬 `clear_all`에서 `chunk_vectors`도 삭제 + main.js 직접 삭제 경로에도 동일 반영 필요.

### 2.4 성능 — 전체 재렌더 + 폴링 병행 (settings.html, dashboard.html)
- cache-event의 discovered/done/error마다 `refreshCache()` 전체 목록(최대 500행) DOM 재생성 (settings.html:795,808,813)
- 그 위에 5초(settings:824)/6초(dashboard:465) 폴링이 또 전체 갱신
- 백필 스캔처럼 이벤트가 몰릴 때 IPC(list) + DOM 재생성 폭주 → 깜빡임·버벅임

`updateRowFromEvent`(행 단위 갱신)가 이미 있으므로 done/error도 행 단위로 처리하고, 전체 갱신은 discovered 1회 + 낮은 빈도(예: 30초) 폴백으로 제한 권장. `refreshCache` 디바운스도 효과적.

## 3. 심각도 중간

### 3.1 main.js 갓파일 — 책임 8개 혼재
데몬 2종 관리, 창 5종, 문서 파서(docx XML 정규식 파싱, xlsx 읽기), 다운로드 2벌, 설치기까지 한 파일. 모듈 분리 권장: `lib/ndjson-daemon.js`, `lib/windows.js`, `lib/config.js`, `lib/file-readers.js`, `lib/model-download.js`, `lib/llamacpp-setup.js`.

### 3.2 NDJSON 데몬 관리 코드 중복
Observer(startDaemon/onDaemonStdout/sendCommand, 104-217)와 Cache(startCacheWorker/onCacheStdout/sendCacheCommand, ~270-370)가 spawn·라인 버퍼링·pending 맵·타임아웃까지 거의 동일한 구현 2벌. 공용 클래스 1개로 통합하면 ~120줄 절감 + 동작 일관성(타임아웃 8초 vs 600초 같은 차이가 명시적 옵션이 됨).

### 3.3 다운로드 스트리밍 코드 중복
`downloadToFile()`(978-1007, llama.cpp용)과 `runDownload()`(1142-1226, 모델용)가 진행률·backpressure 처리까지 같은 로직 2벌. 모델용은 `.part` 임시파일·취소 지원이 있으므로 이를 기준으로 통합 가능.

### 3.4 확장자·차단 목록 이중 정의 (JS ↔ Python 드리프트)
`CACHE_EXTS`/`BLOCKED_NAMES`(main.js:235-253) ↔ `router.TEXT_EXTS/IMAGE_EXTS/BLOCKED_NAMES`(Python). 새 형식 추가 시 두 곳을 고쳐야 하고 이미 누락되면 조용히 어긋남. main.js 필터는 최적화일 뿐이므로(워커도 스킵 판단함) 제거하거나, 워커 ready 시 지원 목록을 넘겨받는 방식 권장.

### 3.5 설정 기본값 이중화 (main.js ↔ config.py)
둘 다 `DEFAULT_CONFIG`를 갖고 같은 leva-config.json을 공유하지만 키 집합이 다름 — `nCtx`, `nGpuLayers`, `nThreads`, `flashAttn`, `pdfMaxPages`, `embedChunkChars` 등은 Python에만 있어 **UI에서 변경 불가**. 노출할 것과 내부값을 정리하고 한쪽을 소스로 삼을 것.

### 3.6 settings ↔ dashboard 캐시 이벤트 코드 100% 중복
`onCacheEvent` 핸들러, `updateRowFromEvent`, `applyRowState`, tally/busy 표시 로직이 두 파일에 그대로 복사됨. 그 외 `escapeHtml`(3곳), `docxHtml`·`xlsxTableHtml`(2곳), `renderFileInto`(2곳), 확장자 배열(2곳), CSS 변수·컴포넌트(~150줄)도 중복. 공용 `shared.js`/`shared.css`로 추출 시 **~400줄 절감**.

### 3.7 워커 이벤트 필드 활용 불일치
- `gather-done`: 어떤 창도 처리 안 함 (백필 수집 완료를 알릴 좋은 시점인데 버려짐)
- `summary`, `chunks`: 이벤트로 오지만 미사용
- HUD(renderer.js:305-320)는 start/done/error만 처리 — progress/embedded 무시라 진행感 없음. 반대로 **백필 스캔 시 파일마다 start/done 말풍선**이 떠서 대량 스캔이면 스팸 (MAX_BUBBLES로 제한되긴 함). discovered/gather-done을 활용해 "N개 파일 캐싱 중 → 완료 (M성공/K실패)" 집계 말풍선으로 바꾸는 것을 권장.

## 4. 심각도 낮음

- **stderr 무시**: observer·cache 워커의 stderr를 버림(main.js:133, 303) — 파이썬 쪽 `_log`가 전부 유실. 최소한 개발 모드에선 콘솔로.
- **침묵 catch**: `catch (e) {}` 다수(설정 저장 실패, gather/scan 실패 등) — 사용자에게 실패가 전달 안 됨.
- **오류 표시 불일치**: 오류 잘림 120자(renderer) vs 60자(settings/dashboard); 키워드 4개 고정 slice(renderer.js:313).
- **목록 500개 상한 무표시**: settings.html:506 — 초과분이 조용히 숨겨짐. "외 N개" 표기 권장.
- **cache-list 이중 구현**: main이 node:sqlite로 DB 직접 읽기 + 워커 list 폴백. 편의는 있으나 스키마를 아는 곳이 2곳(파이썬 마이그레이션 시 주의; `SELECT *` 방어는 돼 있음).
- 리스너 중복 등록 우려(settings/dashboard onCacheEvent)는 창이 닫힐 때 렌더러 프로세스와 함께 파기되므로 **실제 누수는 아님** — 다만 같은 페이지에서 재등록하지 않는 현 구조 유지 필요.

## 5. 권장 리팩토링 로드맵

**1단계 — 버그·보안 (즉시)**
XSS 2건 수정(2.1, 2.2), clear 시 chunk_vectors 삭제(2.3, 파이썬+main 두 경로), 오류 표시 통일.

**2단계 — 성능**
이벤트 기반 행 단위 갱신으로 전환, refreshCache 디바운스, 폴링 30초 폴백으로 축소(2.4).

**3단계 — 구조**
main.js 모듈 분할(3.1), NDJSON 데몬 클래스 통합(3.2), 다운로드 통합(3.3), 렌더러 공용 shared.js/css 추출(3.6). 예상 절감 ~700줄.

**4단계 — 정합성·UX**
확장자 목록 단일화(3.4), 설정 키 정리(3.5), HUD 집계 말풍선 + gather-done 활용(3.7), 500개 상한 표시.

## 6. 총평

보안 기본 설정(contextIsolation, preload 브리지)과 이벤트 우선 설계 등 뼈대는 건실함. 문제는 (a) 성장하며 한 파일에 눌어붙은 main.js와 창 간 복사-붙여넣기 중복, (b) JS↔Python 이중 정의로 인한 드리프트 위험, (c) 이벤트가 있는데도 전체 재렌더+폴링에 의존하는 UI 갱신. 위 1·2단계는 리스크가 낮고 효과가 커서 먼저 처리할 가치가 있음.
