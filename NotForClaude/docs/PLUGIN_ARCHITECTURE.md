# AISummary 플러그인 아키텍처 (v1 계약)

> 이 문서는 플러그인 시스템의 **바꾸기 어려운 계약(contract)** 이다. 매니페스트
> 스키마·호스트 API·권한 모델은 한 번 배포되면 하위호환을 지켜야 하므로, 여기 적힌
> 불변 원칙과 v1 표면을 기준으로 구현을 확장한다.

---

## 0. 철학 & 불변 원칙 (Invariants)

이 제품은 **프라이버시 우선 · 100% 로컬 · 저사양** 이 존재 이유다. 플러그인은 절대
이 약속을 깨면 안 된다. 아래 5개는 **영구 불변**이다.

1. **egress 함수 없음.** 플러그인에 네트워크·임의 경로 파일쓰기를 주지 않는다.
   → 악성 플러그인이 샌드박스 안에서 무엇을 하든 **데이터를 밖으로 뺄 통로가
   존재하지 않는다.** 위험이 "데이터 유출(치명)"에서 "품질/자원(관리 가능)"으로 내려간다.
2. **플러그인 = 데이터 우선(선언형).** 임의 코드는 오직 WASM 샌드박스에서만 돈다.
   인프로세스 파이썬 실행이나 raw 서브프로세스는 채택하지 않는다.
3. **UI는 선언형 컴포넌트 트리만.** 플러그인은 raw HTML/JS를 주입할 수 없다.
4. **플러그인은 자기 표면(모드)만 소유.** 상단 nav·설정·권한 동의창·전역 크롬은
   앱이 소유하며 항상 최상단이다. (피싱·스푸핑 방지)
5. **능력(capability)은 3층.** 매니페스트가 *요청* → 사용자가 *허가* → 런타임이 *강제*.

---

## 1. 실행 티어

| 티어 | 형태 | 상태 | 설명 |
|------|------|------|------|
| **T0** | 선언형(declarative) | **구현됨** | 페르소나/스키마/지식팩/flow-as-data. 코드 실행 0 → 유출 불가. |
| **T1** | flow-DSL | **구현됨** | 안전한 다단계 로직(검색→변형→생성→되묻기)을 *데이터*로 기술. **무컴파일 저작 경로.** |
| **T2** | WASM | **구현됨** | 매개 호스트 API + 능력부여. **임의 코드는 오직 여기서만.** 네트워크 import 미제공 → 유출 불가. |

- 대부분의 플러그인(요약·추출·적대적 리허설 등)은 **T0+T1로 충분**하다.
- T2(WASM)는 커스텀 파싱/계산 등 진짜 코드가 필요한 20%만. 저자는 Rust/AssemblyScript/
  TinyGo 등으로 작성해 WASM으로 컴파일한다. **첫 T2 플래그십 = 인터랙티브 소설 메이커**
  (`plugins/builtin/novel-maker`, AssemblyScript). §6 참조.

---

## 2. 매니페스트 v1 (`plugin.json`)

```jsonc
{
  "manifestVersion": 1,
  "id": "author.plugin",          // 네임스페이스 id (마켓 충돌 방지)
  "name": "표시 이름",
  "version": "0.1.0",             // semver
  "author": "이름",
  "description": "한 줄 설명",
  "engines": { "aisummary": ">=0.4" },   // 앱 버전 호환
  "permissions": {                        // 요청 능력 (사용자가 허가)
    "folders": ["*"],                     // documents:read 스코프
    "network": false,                     // 항상 false (불변)
    "capabilities": ["model:generate", "ui:render", "storage:local"]
  },
  "contributes": { /* §3 */ },            // 열린 맵 (모르는 타입은 무시)
  "entry": { "wasm": "plugin.wasm", "sha256": "..." },  // T2만
  "signature": "..."                      // verified 등급용 (§4)
}
```

- **호환성 규칙:** 로더는 모르는 `contributes` 타입·필드를 **조용히 무시**한다
  (forward-compat). 그래서 새 기여 타입을 추가해도 옛 앱이 안 깨진다.

---

## 3. 기여 지점 (`contributes`)

| 키 | 상태 | 설명 |
|----|------|------|
| `mode` | **구현됨** | 하나의 앱 "모드". `label, icon, placeholder, persona, surfaces`. |
| `knowledgePack` | 계획 | 동봉 오프라인 지식팩 → 플러그인별 격리 색인 (§7). |
| `flow` | 계획 | T1 다단계 스텝 트리. |
| `extraction` | 계획 | 구조화 필드 스키마 + GBNF 문법 강제(작은 모델도 안정적). |
| `command` | 계획 | 퀵액션(Alt+Space·버튼). |
| `trigger` | 계획 | 파일 이벤트 훅(사용자 있을 때). |

지금은 스키마에 6개를 **선언만** 해두고 구현은 점진(mode → flow → knowledgePack 순).

### 3.1 flow-DSL v1 (잠금)

flow = **선언형 데이터**(코드 아님)이며, **호스트(우리)가 프론트엔드에서 해석**한다.
플러그인은 로직을 코드로 실행하지 않고 "무엇을 할지"만 기술한다 → T1은 여전히 유출 불가.

**구조:** `{ start: "stepId", steps: { <id>: <step> } }` + 실행 중 `state`(변수) + `{{var}}` 템플릿.

**스텝 타입 v1 (이 세트로 잠금):**

| 스텝 | 필드 | 동작 |
|------|------|------|
| `text` | `text`, `next` | 고정 지문 출력(마크다운 + `{{var}}` 템플릿). LLM 호출 없음 |
| `set` | `vars{}`, `next` | 상태 변수 설정. 값은 문자열(=`{{var}}` 템플릿) 또는 `{expr:"..."}`(=수식 평가) |
| `generate` | `prompt`, `grounded`, `into`, `next` | LLM 호출. `grounded:true`=검색+인용(폴더스코프), `false`=자유 생성(무제한). 결과를 `into`에 저장하며 화면에 스트리밍 |
| `retrieve` / `packSearch` | `query`, `into`, `next` | 문서/지식팩 검색 결과를 상태에 |
| `choice` | `prompt?`, `options[{label,set?,goto}]` | 선택지 버튼 → 고르면 `set` 적용(`{expr}` 지원) 후 `goto` |
| `input` | `label?`, `into`, `goto` | 유저 텍스트 입력을 변수에 |
| `render` | `ui[]`, `next` | 컴포넌트 트리(상태 반영)를 표면에 |
| `if` | `cond{expr}` 또는 `cond{var,equals}`, `then`, `else` | 상태 기반 분기. `expr`는 수식 평가, `var/equals`는 동등 비교 |
| `goto` | `to` | 점프 |
| `end` | — | 종료 |

프로(적대적 리허설)와 크리에이티브(인터랙티브 소설) 두 플래그십이 **같은 세트**로 표현된다.
`generate.grounded` 플래그 하나가 "문서 근거 인용" ↔ "무제한 자유 생성"을 가른다.

#### 3.1.1 스크립팅(안전 수식) — `expr`

`set.vars`의 값과 `if.cond`에 `{expr:"..."}` / `expr`를 쓰면 상태 기반 계산·조건이
가능하다. **`eval`/`Function`을 절대 쓰지 않고**, 호스트가 자체 토크나이저 +
재귀하강 파서(`flowEval`)로 해석한다. 지원 범위(이걸로 잠금):

- 값: 숫자, 문자열(`'..'`/`".."`), 식별자(→ 현재 `state[변수]`, 숫자면 숫자로 승격)
- 산술 `+ - * / %`, 문자열 결합(`+`), 단항 `-`/`!`
- 비교 `< > <= >= == !=`, 논리 `&& ||`, 괄호 `( )`

파서는 정해진 문법만 인식하므로 임의 코드 실행·부작용·전역 접근이 **구조적으로 불가**하다.
예: `{ "hp": {"expr":"hp-30"} }`, `if.cond {"expr":"hp<=40 || sanity<=40"}`.

#### 3.1.2 상태창(`status`) — 라이브 캐릭터 패널

`mode.status`를 선언하면 호스트가 플러그인 **사이드바 표면**에 상태창을 렌더한다.
플로우가 `set`/`input`/`generate`로 상태를 바꿀 때마다 자동 갱신된다.

```
"status": {
  "title": "주인공 상태",
  "rows": [
    { "label": "이름", "value": "{{name}}" },
    { "label": "체력", "value": "{{hp}}", "bar": { "max": 100 } }
  ]
}
```

각 `row`는 `label` + `value`(`{{var}}` 템플릿). `bar.max`가 있으면 진행 막대를
그리고 잔량에 따라 색을 바꾼다(≤25% 적색 · ≤50% 황색 · 그 외 강조색). 상태창은
플러그인 데이터로만 정의되며, 호스트가 우리 컴포넌트로만 그린다 → 여전히 유출 불가.

#### 3.1.3 `stats` — 선언형 스탯(초기화 + 상태창 자동 생성)

`mode.stats`에 스탯을 선언하면 두 가지가 자동으로 처리된다:

```
"stats": [
  { "key": "hp", "label": "체력", "initial": 100, "max": 100, "bar": true, "show": true }
]
```

- 플로우 시작 시 `state[key] = initial` 로 **초기 상태를 시드**한다(별도 `set` 불필요).
- `show:true` 인 스탯으로 **상태창(§3.1.2)을 자동 구성**한다(`status`를 직접 쓰면 그게 우선).

이후 `set`/`choice.set`의 `{expr}`(§3.1.1)로 스탯을 증감하고, `if.cond.expr`로 분기한다.
이 세 가지(`stats` + `expr` + `status`)가 **인터랙티브 소설 메이커의 저작 모델**이다.

---

## 3.2 인터랙티브 소설 메이커 (Interactive Novel Maker) — **T2 WASM 플러그인**

저작 결과의 네이티브 포맷이 곧 flow-DSL 이라 *저장 = 런타임 = export* 가 무손실로
일치한다. **메이커 자체가 코어 앱 기능이 아니라 T2 WASM 플러그인**이다
(`plugins/builtin/novel-maker`, AssemblyScript → `plugin.wasm`). 즉 "이 정도 복잡한
저작 도구도 플러그인 시스템 위에서 만들 수 있다"를 스스로 증명한다. flow-DSL 런타임은
**플레이어**로 재활용하되(호스트가 소유), 메이커의 상태·편집 로직은 전부 WASM 안에서 돈다.

- **UI**: WASM이 컴포넌트 트리(JSON)를 `host_ui_render`로 내보내고 호스트가 우리
  컴포넌트로만 그린다. 이벤트는 `on_event(handler, value)`로 되돌아온다(§6.1).
- **저작 방식**: (a) AI 초안 — `host_generate`로 로컬 LLM 호출 → 반환 텍스트를 플러그인이
  파싱해 분기 장면으로 조립(약한 모델 대비 스켈레톤 폴백). (b) 수동 노드 편집 — 장면 카드마다
  유형(text/generate/choice/input/if/end) · 다음 연결 · 선택지 · 스탯 효과.
- **스탯/상태창**: 유저가 스탯 변수를 정의(§3.1.3) → 플레이 중 상태창 표시, 선택지 효과·조건 분기에 사용.
- **결과물**: 로컬 저장(`host_storage_save` → `~/.aisummary/plugin-data/novel-maker/<id>.json`) ·
  **플러그인으로 설치**(`host_export` → 완성 매니페스트를 새 플러그인으로 기록, 자동 enable).
  설치된 소설도 `network:false` → 유출 불가 승계.
- **미리보기**: `host_preview`로 편집 중 프로젝트를 호스트 flow-DSL 런타임에 넘겨 즉시 플레이.

`text` 스텝(§3.1 표)과 `stats`(§3.1.3)가 이 저작 모델의 핵심 프리미티브다.

---

## 6. T2 실행 모델 — WASM 샌드박스 + 매개 호스트 API

**원칙:** 플러그인은 `.wasm` 코드를 싣는다. 호스트가 인스턴스화하며 **주입하는 import는
아래 매개 함수뿐**이다. 네트워크/FS import가 애초에 없으니 WASM은 계산·저장·렌더는 해도
**외부로 내보낼 통로가 존재하지 않는다 → 유출 불가(구조적 보장).**

### 6.1 ABI (호스트 ↔ WASM)

문자열은 UTF-8로 선형 메모리에 주고받는다. WASM은 `alloc(size)`/`dealloc(ptr,size)`와
`memory`를 export 한다.

- **호스트 → WASM (export 호출):**
  `on_init(ptr,len)` (저장소 스냅샷 `{storage:{key:value}}`), `on_event(hPtr,hLen,pPtr,pLen)`
  (핸들러 문자열 + 값), `on_generate_done(reqId,ptr,len)` (생성 결과).
- **WASM → 호스트 (import 호출, 이게 능력의 전부):**
  `host_ui_render(ptr,len)` (컴포넌트 트리 `{surface,body[]}`),
  `host_generate(reqId,ptr,len)` (`{prompt,persona}` — 결과는 `on_generate_done`으로),
  `host_storage_save/delete(ptr,len)`, `host_export(ptr,len)` (완성 매니페스트 설치),
  `host_preview(ptr,len)` (`{persona,stats,flow}`를 호스트 런타임이 플레이), `host_toast/log`.
  **`host_fetch`류는 없음.**

호스트는 모든 WASM 진입 호출을 try/catch로 감싸 abort 시에도 앱 전체가 죽지 않는다.

### 6.2 범용 호스트 API (백엔드) — 모든 플러그인 공용

novel 전용 엔드포인트를 걷어내고 **범용 host API**로 일반화했다. 어떤 티어의 플러그인이든 쓴다.

- `POST /host/generate/stream` — `model:generate` (무근거/근거, 페르소나 인라인, 추론 큐 경유)
- `GET/POST /host/storage/{pid}/{list,load,save,delete}` — `storage:local`. 플러그인별
  격리 디렉터리(`~/.aisummary/plugin-data/{pid}/`). 경로 핸들은 절대 안 준다.
- `POST /host/plugins/export` — 플러그인이 만든 매니페스트를 새 플러그인으로 설치.
  기록 시 `network:false` 강제.
- `GET /plugins/{pid}/asset?file=` — 플러그인 폴더 내부 자산(예: `plugin.wasm`)만 안전하게 서빙.

빌드: `plugins/builtin/novel-maker/src/main.ts`(AssemblyScript) → `plugin.wasm`.
`plugins/builtin/novel-maker/BUILD.md` 참조.

---

## 4. 권한 & 신뢰

**능력 목록(capabilities)** — 각 호스트 네임스페이스에 대응:

- `documents:read` — `docs.retrieve` (허가 폴더 스코프)
- `knowledgePack` — `docs.packSearch` (자기 팩)
- `model:generate` — `model.generate`
- `ui:render` — 메인 뷰 렌더 / `ui:sidebar` — 사이드바 패널 / `ui:window` — 새 창(v2)
- `storage:local` — `storage.*` (플러그인 전용 폴더)
- ~~`network`~~ — **제공하지 않음(불변)**
- `chat:observe` — (고권한·나중) 앱 메인 채팅 답변 구독. verified 전용.

**신뢰 등급:**

- `builtin` (1st-party, 앱 동봉) > `verified` (서명·심사) > `community` (무서명).
- community라도 **네트워크는 애초에 없으므로** 유출 위험은 구조적으로 0.
  대신 리소스 남용·품질 문제를 리뷰/제한으로 관리.

**부여 흐름:** enable 시 권한 시트 표시 → 허가 저장. 업데이트가 권한을 **더**
요구하면 **권한 diff 재동의**.

---

## 5. 호스트 API v1 (우리가 제공하는 SDK)

플러그인이 호출할 수 있는 함수는 이게 전부다. 능력별로 import가 연결되며, 허가되지
않은 함수는 아예 링크되지 않아 호출 시 trap 된다.

```
model.generate(prompt, { system, temperature }) -> text
    // system = 시스템프롬프트 지정, 반환값 = AI 답변. 추론 큐 경유.
    // 자기가 부른 호출의 결과만 받는다 (앱 대화 구독 아님).
model.generateStream(prompt, opts, onToken)         // v2 (호스트→guest 콜백)

docs.retrieve(query, top_k) -> [{ text, filename, page, score }]
    // 허가 폴더 + 자기 지식팩 네임스페이스 안에서만. 경로 핸들은 주지 않음.
docs.packSearch(query, top_k) -> [...]

ui.render(surface, tree)        // surface ∈ "main" | "sidebar"
    // 우리 컴포넌트 트리만. 이벤트는 { onClick: "handlerId" } → guest 핸들러 호출.
ui.openWindow({ title, w, h }, tree)                // v2

storage.save(name, data, { format })   // format ∈ "json" | "bytes"
storage.load(name) -> data
storage.list() -> [name]
storage.remove(name)
    // 경로는 ~/.aisummary/plugins/<id>/data/ 로 고정. 임의경로 금지. 용량 상한.

log(msg)
emit(structured)                       // 최종 구조화 결과 반환
```

**절대 없음:** 네트워크, 임의 경로 파일 IO, 프로세스 실행(exec), 환경변수, 시스템 콜.

**컴포넌트 v1:** `Text, Markdown, Input, Button, Select, List, Table, Divider, Progress`.

---

## 6. UI 표면 & 프로토콜

**플러그인 = 하나의 모드.** 채팅/회의와 동일한 격으로 **두 개의 자기 패널**을 소유:

- **메인 콘텐츠 뷰** (`view-plugin` 자리) — 플러그인 UI를 그린다.
- **전용 사이드바 패널(선택)** — 채팅의 "대화 목록", 회의의 "회의 목록"처럼 자기
  세션/항목 리스트.

**앱이 항상 소유 (플러그인 접근 불가):** 상단 nav(모드 전환), 설정 모달, **권한
동의 다이얼로그(앱-모달·최상단)**, 감시폴더 등 전역 크롬. 모든 플러그인 표면엔
**"플러그인: X" 배지**를 강제한다.

**로직/UI 분리 (Figma·VSCode 패턴):**

```
WASM/선언형 로직(백엔드)  ──render(tree)──►  프론트엔드가 우리 컴포넌트로 렌더
        ▲                                              │
        └──────────── 이벤트(handlerId) ◄──────────────┘
```

플러그인 로직은 UI DOM을 직접 만지지 않는다. 선언형 트리를 보내고, 이벤트만 메시지로
받는다. → 스푸핑·XSS·유출 벡터가 원천 차단.

---

## 7. 지식팩 (도메인 지식 언락)

- 플러그인이 `pack/` 폴더(문서/조항/JSON)를 동봉 → enable 시 **플러그인별 격리
  벡터 네임스페이스**로 색인. uninstall 시 함께 삭제(유저 개인 인덱스 오염 없음).
- 쿼리 시 검색 범위 = **[허가 폴더 ∩ 유저 문서] + [플러그인 팩 네임스페이스]**.
  → "적대적 리허설"이 내 계약서 + 표준 취약점 조항을 함께 근거로 삼는다.
- 팩은 **다운로드-인만** 허용(공개 지식 들여오기). 유저 데이터는 여전히 무유출.
- "전문성은 모델 가중치가 아니라 **데이터**에 산다"의 구현체.

---

## 8. 모델 오케스트레이션

- 플러그인이 `model: { tier }` 선언 → 런타임이 필요한 소형 모델을 **추론 큐로
  하나씩 스왑**(동시 실행 아님, 저사양 보호).
- 번들 모델/LoRA는 크기 문제로 후순위.

---

## 9. 자원 제한 (저사양 필수)

- WASM: **fuel/epoch로 CPU 상한**, **선형 메모리 상한(예: 64–128MB)**, 타임아웃.
- 모든 추론은 **직렬 큐** 통과. 폭주 플러그인이 약한 PC를 못 잡아먹게.

---

## 10. 생명주기

```
설치(폴더 드롭 / .aisplugin zip)
  → 매니페스트 검증 (+ verified면 서명 검증)
  → enable (권한 동의)
  → 실행 (호스트 API, 큐, 리소스 제한)
  → 업데이트 (권한 diff 재동의)
  → disable / uninstall (팩·데이터 함께 제거; builtin은 삭제 불가)
```

---

## 11. 로드맵 (현재 vs 계획)

**구현됨 (walking skeleton)**
- T0 선언형 플러그인, 로더/레지스트리(builtin + user 스캔), 매니페스트 검증
- 권한 상태 저장, **폴더 스코프 강제**(`_scope_hits`), 페르소나 주입 이음새
- `mode` 기여(고정 UI), 설정 "플러그인" 관리탭(새로고침·폴더 열기·활성화·삭제·권한 표시)

**다음**
- flow-DSL(T1) 명세 + 실행기
- knowledgePack 격리 색인
- UI 컴포넌트 렌더러 + 표면 프로토콜(main/sidebar), 배지
- 호스트 API 나머지(storage, docs.packSearch)

**나중**
- WASM 런타임(wasmtime) + 매니페스트 `entry.wasm` + 능력별 import 링크
- 서명/verified 등급, 마켓플레이스
- `model.generateStream`, `ui.openWindow`, `chat:observe`(고권한)

---

*이 문서의 §0 불변 원칙과 §2/§5 계약(매니페스트·호스트 API v1)은 하위호환 대상이다.
변경이 필요하면 `manifestVersion` 을 올리고 마이그레이션 경로를 명시한다.*
