# STT 회의 요약 기능 — 아키텍처 리포트

작성일: 2026-07-03 · 대상: LevA (온디바이스, Windows, llama.cpp 기반)

---

## 1. 결론 먼저 (TL;DR)

**2단계 파이프라인을 권장한다: STT(whisper.cpp) → 화자 분리(sherpa-onnx) → 정리(기존 Gemma Agent).**

- STT 전용 모델로 텍스트를 뽑고, 요약·정리는 이미 있는 텍스트 모델(Gemma) 파이프라인을 재사용한다.
- 오디오를 직접 이해하는 LLM(Voxtral 등)을 쓰는 단일 모델 방식은 아직 회의 요약 용도로 부적합하다(아래 2절).
- 화자 분리는 sherpa-onnx(ONNX Runtime, CPU)로 처리한다 — PyTorch 없이 수 MB 모델로 동작해 배포에 유리하다.
- 인물 "이름" 매핑은 3단계 전략: ① 화자 자동 분리(화자1/화자2) → ② LLM이 대화 내용에서 이름 추정 → ③ 사용자가 UI에서 수동 확정.

---

## 2. 아키텍처 선택: 오디오 LLM 직접 vs STT + Agent 2단계

### A안 — 오디오 이해 LLM 단일 모델 (예: Voxtral)

llama.cpp가 멀티모달(mtmd)로 오디오 입력을 지원하고, Voxtral Mini 3B GGUF도 공개되어
있어 기술적으로는 우리 스택(llama-server)에 바로 얹을 수 있다.

| 항목 | 평가 |
|---|---|
| 스택 호환 | ◎ llama-server 그대로, 모델만 추가 |
| 긴 회의(30분~2시간) | ✕ 오디오가 30초 고정 청크로 처리됨 — 1시간 회의는 컨텍스트·품질 모두 한계 |
| 화자 분리 | ✕ 불가능(모델이 화자 개념을 출력하지 않음) |
| 전사 정확도 | △ Voxtral Transcribe 2는 WER 우수(FLEURS 평균 5.9%)지만 GGUF 로컬 추론은 검증 부족 |
| 환각 위험 | △ "전사"가 아니라 "생성"이라 없는 말을 지어낼 수 있음 — 회의록 신뢰성에 치명적 |

### B안 — STT → Agent 2단계 (권장)

```
오디오 파일(mp3/m4a/wav)
  → ① STT: whisper.cpp(whisper-server)  ....... 타임스탬프 있는 전사 텍스트
  → ② 화자 분리: sherpa-onnx  ................. 구간별 화자 라벨 → ①과 시간축 병합
  → ③ 정리: 기존 Gemma 파이프라인  ............ 회의록(안건/결정/액션아이템) + 키워드 + 임베딩
```

| 항목 | 평가 |
|---|---|
| 전사 정확도 | ◎ Whisper 계열은 3년간 검증된 사실 기반 전사. 환각 최소 |
| 긴 회의 | ◎ VAD 기반 세그먼트 처리로 길이 제한 없음 |
| 화자 분리 | ◎ 전용 단계로 처리, STT와 시간축 병합 |
| 기존 자산 재사용 | ◎ 요약·키워드·임베딩·검색·근거 표시 전부 기존 3단계 파이프라인 그대로 |
| 유지보수 | ◎ 각 단계 독립 교체 가능(STT 모델만 업그레이드 등) |

**결론: B안.** 결정적 이유는 세 가지 — 화자 분리가 A안에서는 원천 불가능하고, 회의록은
"지어내지 않는 전사"가 생명이며, 우리의 요약·검색 파이프라인이 이미 텍스트 기준으로
완성되어 있어 STT만 붙이면 나머지가 공짜다.

---

## 3. STT 엔진 선택 (한국어 기준)

| 후보 | 한국어 | 속도 | 배포(Windows) | 비고 |
|---|---|---|---|---|
| **whisper.cpp + large-v3-turbo** (권장) | ◎ | CPU 가능, CUDA/Vulkan 빌드 제공 | ◎ exe 단일 배포, **whisper-server**(HTTP) 제공 | llama.cpp와 같은 GGML 생태계. 우리의 "GitHub 릴리스 자동 설치" 인프라 재사용 가능 |
| faster-whisper (CTranslate2) | ◎ | NVIDIA에서 최속(~12× RT, whisper.cpp CUDA ~8×) | △ Python 의존(PyInstaller 번들 커짐) | GPU 확정 환경이면 우수 |
| SenseVoice-Small | ◎ (한중일영+광둥) | ◎◎ 비자기회귀라 Whisper-Small 대비 5배+, ONNX 지원 | ○ sherpa-onnx로 구동 가능 | 저사양 CPU 대안. 타임스탬프 정밀도는 Whisper보다 거침 |
| Voxtral (GGUF) | ○ | △ | ◎ | 2절 사유로 제외 |

**권장: whisper.cpp의 `whisper-server` + `large-v3-turbo` 모델(GGUF, 약 1.6GB Q8/809MB Q5).**

- 이유 1: 이미 LevA는 llama.cpp 릴리스를 자동 다운로드·설치하고 `llama-server`를
  LlamaManager로 관리한다. whisper.cpp도 동일 패턴(릴리스 zip → `whisper-server.exe` →
  HTTP `/inference`)으로 **WhisperManager를 복붙 수준으로 추가**할 수 있다.
- 이유 2: CPU만 있는 사용자도 turbo 모델이면 실용 속도(대략 실시간 대비 2~4×)가 나온다.
- 저사양 폴백: 설정에서 `small`/`base` 모델 선택지 제공. 추후 SenseVoice(sherpa-onnx) 옵션 추가 여지.
- 오디오 포맷: whisper.cpp는 wav(16kHz) 입력이 기본 — **ffmpeg 정적 바이너리 1개를 함께
  설치**해 mp3/m4a/웹엑스 녹음 등을 wav로 변환한다(자동 설치 인프라 재사용).

---

## 4. 인물(화자) 구분 — Speaker Diarization

### 원리

화자 분리는 STT와 별개의 문제다. 표준 파이프라인은:

```
VAD(발화 구간 검출) → 구간별 화자 임베딩 추출 → 클러스터링(화자 수 추정) → 구간별 화자 라벨
```

그 결과(`00:03:10~00:03:42 = SPEAKER_01`)를 STT의 타임스탬프와 교차 병합하면
"화자별 대사"가 나온다. 단어 단위 정밀 정렬(WhisperX 방식)까지는 회의록 용도에 과하고,
**세그먼트 단위 병합이면 충분**하다.

### 구현 옵션 비교

| 옵션 | 품질(DER) | 배포 관점 | 판단 |
|---|---|---|---|
| **sherpa-onnx** (pyannote segmentation-3.0 ONNX + CAM++/3D-Speaker 임베딩) | 12~15% | ◎ ONNX Runtime만 필요, 모델 수 MB~수십 MB, CPU 동작 | **권장.** PyTorch 없이 동작 — 실제로 로컬 회의록 앱(OpenWhispr)이 같은 조합 사용 |
| pyannote-audio 3.1 (PyTorch) | 11~19% (기준선) | ✕ torch 런타임 ~1.5GB, GPU 사실상 필요 | 품질 기준선이지만 Electron 배포에 부적합 |
| WhisperX (faster-whisper+wav2vec2+pyannote) | 우수(단어 정렬까지) | ✕ torch 의존 동일 | 서버형 제품용. 우리에겐 과함 |
| LLM에게 맡기기(전사 텍스트만 보고 화자 추정) | 낮음 | ◎ | 보조 수단으로만(아래 이름 매핑) |

주의할 한계: **겹쳐 말하기(overlap)** 구간은 어떤 모델도 잘 못 잡는다(업계 공통).
회의록 UI에 "화자 불확실" 표시를 두는 것이 정직한 설계다.

### 화자 → 실제 이름 매핑 3단계

1. **자동 분리**: 위 파이프라인이 `화자1, 화자2, …`까지 만든다 (이름은 모름).
2. **LLM 추정**: 정리 단계에서 Gemma에게 "대화 중 호칭·자기소개('김 부장님', '저는 OO입니다')를
   근거로 화자N의 실명 후보를 추정하되, 근거 없으면 화자N 유지"를 지시. 추정 근거를 함께 출력.
3. **수동 확정(UI)**: 회의록 뷰에서 화자 칩 클릭 → 이름 입력/수정 → 전체 치환.
   (선택) **내 목소리 등록**: 사용자가 10초 샘플을 등록하면 화자 임베딩 코사인 매칭으로
   "나"를 자동 라벨링 — sherpa-onnx speaker-identification으로 가능. MVP 이후 권장.

---

## 5. LevA 통합 설계

기존 3단계 캐싱 파이프라인에 자연스럽게 흡수된다:

```
router: .mp3 .wav .m4a .ogg .webm .flac → "audio"
1단계(본문 준비): ffmpeg → wav → whisper-server(STT) + sherpa-onnx(화자)
                  → "[00:03:12] 화자1(김부장?): 다음 분기 예산은…" 형식의 전사 본문
                  → ocr_text 컬럼 재사용(전사 저장 — 재시도·채팅에서 재사용)
2단계(임베딩): 기존 그대로 (전사 청크 → BGE-M3 → 의미검색·근거 표시까지 공짜)
3단계(정리): 회의 전용 프롬프트로 분기 —
             참석자 / 안건별 논의 / 결정사항 / 액션아이템(담당·기한) / 미결 이슈
```

- **프로세스 구조**: `WhisperManager`를 `LlamaManager`와 같은 패턴으로 추가
  (별도 포트, Job Object로 수명 관리 — 이미 만든 정리 로직이 그대로 적용됨).
- **VRAM/RAM 전략**: STT(1단계)와 Gemma(3단계)는 파이프라인상 겹치지 않으므로
  whisper 모델은 1단계 후 내려도 된다(스왑 정책은 기존 vision↔text와 동일).
- **UI**: 라이브러리에 "회의록" 타입 뷰 — 화자 칩(이름 수정), 타임라인, 액션아이템 체크리스트.
  진행 표시는 기존 파이프라인 이벤트("전사 중 12/40분 — 화자 분석 중") 재사용.
- **성능 예상(1시간 회의, 일반 노트북 CPU 기준)**: 전사 15~30분(turbo) + 화자 분리 3~5분 +
  정리 2~5분. GPU(CUDA) 있으면 전사가 5분 내로 줄어든다.

## 6. 라이선스

whisper.cpp·Whisper 모델(MIT), sherpa-onnx(Apache-2.0), pyannote segmentation-3.0
ONNX 변환본(MIT 파이프라인 기준, 원 모델 카드 확인 필요), ffmpeg(LGPL 빌드 선택) —
상용 배포에 문제없는 조합으로 구성 가능.

## 7. 로드맵 제안

| 단계 | 내용 | 산출물 |
|---|---|---|
| M1 (MVP) | 오디오 라우팅 + ffmpeg/whisper.cpp 자동 설치 + 전사 → 기존 3단계 연결 | 화자 없는 회의 요약 |
| M2 | sherpa-onnx 화자 분리 + 시간축 병합 + 회의 전용 정리 프롬프트 | "화자N" 회의록 |
| M3 | LLM 이름 추정 + 화자 칩 수동 확정 UI + 액션아이템 뷰 | 완성형 회의록 |
| M4 (선택) | 내 목소리 등록(화자 식별), 실시간 녹음 전사(마이크·시스템 오디오) | 라이브 회의 비서 |

---

## 참고 자료

- llama.cpp 멀티모달(오디오) 문서: https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md
- Voxtral Mini 3B GGUF: https://huggingface.co/bartowski/mistralai_Voxtral-Mini-3B-2507-GGUF
- Voxtral vs Whisper 2026 벤치마크: https://weesperneonflow.ai/en/blog/2026-03-31-voxtral-whisper-open-source-speech-models-comparison-2026/
- whisper.cpp vs faster-whisper 2026: https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026
- 오픈소스 STT 2026 비교: https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks
- SenseVoice vs Whisper (CJK): https://whispernotes.app/blog/sensevoice-fastest-cjk-transcription
- sherpa-onnx 화자 분리 문서: https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html
- 로컬 화자 분리 실전 사례(OpenWhispr): https://openwhispr.com/blog/local-speaker-diarization
- pyannote speaker-diarization-3.1: https://huggingface.co/pyannote/speaker-diarization-3.1
- 화자 분리 모델 비교 2026: https://brasstranscripts.com/blog/speaker-diarization-models-comparison
- WhisperX 가이드: https://localaimaster.com/blog/whisperx-guide
