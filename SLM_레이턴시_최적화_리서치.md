# 저성능 하드웨어에서 소형 LLM(SLM) 레이턴시 최소화 — 리서치 리포트

**대상:** 로컬 온디바이스 앱(llama.cpp / llama-server, GGUF, Windows), CPU-only 및 소형·내장 GPU 범용
**작성:** 다중 출처 웹 리서치 종합 (2026-07)

---

## 0. 핵심 멘탈 모델 — 지연은 두 단계로 나뉜다

모든 요청의 지연은 성격이 정반대인 두 구간의 합이다. **이 둘을 분리해서 공략해야 한다.**

| 구간 | 하는 일 | 병목 | 무엇에 비례 | 줄이는 법 |
|---|---|---|---|---|
| **프리필(Prefill) = 첫 토큰 지연(TTFT)** | 프롬프트 전체를 한 번에 읽어 KV 캐시를 채움 | **연산(compute) 바운드** | 프롬프트 길이(주의행렬은 길이의 제곱) | 프롬프트 축소, 프리필 캐싱, 스레드/배치, GPU 오프로드 |
| **디코드(Decode) = 토큰 생성 속도(tok/s)** | 토큰을 하나씩 생성, 매 스텝 KV·가중치 전체를 다시 읽음 | **메모리 대역폭 바운드** | 모델 바이트 수(= 파라미터 × 양자화 비트) | 양자화(바이트 축소), 작은 모델, GQA/MoE, 대역폭 큰 하드웨어 |

- 프리필은 연산 바운드, 디코드는 메모리 대역폭 바운드라서 최적화 방향이 다르다. ([Medium/plienhar](https://medium.com/@plienhar/llm-inference-series-5-dissecting-model-performance-6144aa93168f), [Towards Data Science](https://towardsdatascience.com/prefill-is-compute-bound-decode-is-memory-bound-why-your-gpu-shouldnt-do-both/))
- 프리필 지연 ≈ `2·b·s·P / F` (배치 b, 프롬프트 길이 s, 파라미터 P, 연산량 F) → **프롬프트를 2배로 하면 TTFT도 대략 2배.** 긴 프롬프트가 TTFT를 가장 크게 해친다. ([Weka](https://www.weka.io/learn/ai-ml/prefill-and-decode/))
- 디코드 속도 ≈ **`메모리 대역폭 ÷ 모델 바이트 수`.** 매 토큰마다 가중치 전체를 메모리에서 다시 읽기 때문. 그래서 **양자화로 바이트를 줄이면 디코드가 거의 비례해서 빨라진다.** ([finbarr.ca](https://finbarr.ca/how-is-llama-cpp-possible/), [Weka](https://www.weka.io/learn/ai-ml/prefill-and-decode/))
- 배치=1(단일 사용자 온디바이스)에서는 대역폭 병목을 배치로 숨길 수 없다 — 배치를 키워도 한 사용자의 tok/s는 빨라지지 않는다(서버 전체 처리량만 오름). ([finbarr.ca](https://finbarr.ca/how-is-llama-cpp-possible/))
- 실측 예: llama.cpp에서 프리필은 수백 tok/s, 디코드는 수십 tok/s(한 사례: 102K 컨텍스트 프리필 350~400 tok/s vs 생성 ~15 tok/s). CPU 행렬확장 가속은 **연산 바운드인 프리필의 TTFT를 최대 2.9배(Q8)·1.5배(Q4)** 단축. ([Arm Learning Paths](https://learn.arm.com/learning-paths/servers-and-cloud-computing/llama_cpp_streamline/2_llama.cpp_intro/))

> **요약:** TTFT를 줄이려면 → 프롬프트를 짧게 + 프리필 캐싱 + 프리필 병렬화(스레드/GPU). 생성 속도를 올리려면 → 더 작은 양자화 + 더 작은/희소(MoE) 모델 + 대역폭.

---

## 1. 양자화 전략 — 디코드 속도의 가장 큰 지렛대

디코드는 메모리 바운드라 **양자화 비트를 낮출수록 tok/s가 오른다.** 다만 품질·CPU/GPU 특성 트레이드오프가 있다.

### 1-1. 양자화별 속도·품질 (llama.cpp 공식 Llama-3.1-8B 벤치)

| 양자화 | 크기 | 생성 tok/s | 품질(≈FP16 대비) |
|---|---|---|---|
| Q8_0 | 큼 | 50.9 | 사실상 무손실 |
| Q6_K | | 58.7 | 사실상 무손실 |
| Q5_K_M | | 67.2 | 거의 무손실 |
| **Q4_K_M** | ~4.58 GiB | 71.9 | **1% 미만 손실 (권장 기본)** |
| Q4_K_S | | 76.7 | 약간 손실 |
| **IQ4_XS** | ~4.17 GiB | 77.5 | Q4_K_S급 품질을 더 작은 크기로 |

출처: [llama.cpp quantize README](https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md), 품질수치 [Artefact2 KLD gist](https://gist.github.com/Artefact2/b5f810600771265fc1e39442288e8ec9)

- **작은 양자화 = 빠른 디코드**가 표에서 일관되게 확인됨(생성이 대역폭 바운드이므로). ([quantize README](https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md))
- 품질 손실은 4비트 위에서 급감: Q4_K_M의 KL-divergence 0.0075, Q5_K_M 0.0043, Q6_K 0.0032 → **Q5/Q6/Q8은 사실상 무손실**이고 Q4_K_M 위로는 이득이 미미. ([Artefact2](https://gist.github.com/Artefact2/b5f810600771265fc1e39442288e8ec9))
- Q4_K_M는 7B 기준 메모리를 ~16GB→~4GB로 약 75% 절감하면서 품질 손실 1% 미만. ([daily.dev](https://daily.dev/blog/running-llms-locally-ollama-llama-cpp-self-hosted-ai-developers/))

### 1-2. CPU vs GPU에서 갈리는 선택

- **i-quant(IQ4_XS, IQ3_M 등)은 크기 대비 성능이 좋지만 CPU·Apple Metal에서는 K-quant보다 느리고, Vulkan 백엔드와는 아예 비호환.** GPU(CUDA/ROCm)에서 Q4 미만을 노릴 때만 유리. ([bartowski 카드](https://huggingface.co/bartowski/Llama-3.3-70B-Instruct-GGUF), [llama.cpp #5617](https://github.com/ggml-org/llama.cpp/discussions/5617))
- **레거시 Q4_0은 단순 블록 구조라 CPU 생성에 유리할 때가 있다** — 한 64스레드 CPU 벤치에서 생성 tok/s가 Q4_K_M(18.8)보다 Q4_0(39.1)이 빠름. 반대로 프리필은 K-quant가 우세(Q4_K_M이 Q4_0의 147%). ([bartowski 카드](https://huggingface.co/bartowski/Llama-3.3-70B-Instruct-GGUF))
- **온라인 리팩(PR #9921):** 평범한 Q4_0 GGUF를 로드 시 호스트 SIMD(AVX2/AVX512, ARM i8mm/SVE)에 맞게 자동 재배치 → 별도 Q4_0_4_4/4_8/8_8 파일이 불필요해짐. 넓은 SIMD CPU에서 프리필 +33%, 생성 +11% 관측. ([PR #9921](https://github.com/ggerganov/llama.cpp/pull/9921), [bartowski](https://huggingface.co/bartowski/Llama-3.3-70B-Instruct-GGUF))

### 1-3. KV 캐시 양자화 (긴 컨텍스트 메모리 절감)

- `--cache-type-k/-v`(예: q8_0, q4_0)는 **Flash Attention(-fa) 필수.** q8_0은 KV 메모리를 절반으로(품질 거의 무손실, +0.002~0.05 PPL), q4_0은 1/4로(품질 저하 +0.2~0.25 PPL). ([llama.cpp #5932](https://github.com/ggml-org/llama.cpp/discussions/5932), [smcleod](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/))
- K와 V는 민감도가 달라 **K=q8_0, V=q4_0** 조합이 흔한 권장(품질/메모리 균형). ([llama.cpp #5932](https://github.com/ggml-org/llama.cpp/discussions/5932))
- **주의:** KV 양자화는 주로 VRAM 절감용이지 속도용이 아니다 — 한 벤치(30B, 128K)에서 프리필은 f16/q8/q4 동일, q4_0은 110K 지점에서 생성이 ~37% 더 느림(토큰별 역양자화 오버헤드). **메모리가 강제하지 않으면 q8_0을 써라.** ([NVIDIA 포럼](https://forums.developer.nvidia.com/t/kv-cache-quantization-benchmarks-on-dgx-spark-q4-0-vs-q8-0-vs-f16-llama-cpp-nemotron-30b-128k-context/365138))

> **권장:** CPU/범용 기본은 **Q4_K_M**. VRAM 여유 있는 GPU면 Q5_K_M/Q6_K(품질↑). Q4 미만이 필요하고 CUDA/ROCm면 IQ4_XS. Vulkan/Metal/CPU 위주면 i-quant 피하고 K-quant. 긴 컨텍스트 메모리가 부족할 때만 KV를 q8_0로.

---

## 2. llama.cpp / llama-server 실전 파라미터

모든 플래그명은 현행 공식 `tools/server/README.md`로 검증함. ([server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md))

| 플래그 | 효과 | 트레이드오프 |
|---|---|---|
| `-ngl, --n-gpu-layers N` (auto/all 가능) | 레이어를 VRAM에 오프로드 → 디코드/프리필 대폭 단축 | VRAM 초과 시 스필/OOM 또는 느린 CPU 폴백 |
| `-t, --threads N` | 생성 스레드 = **물리 코어 수**로 | 하이퍼스레드까지 넘기면 경쟁으로 오히려 느려짐 |
| `-tb, --threads-batch N` | 프리필용 스레드를 따로 튜닝 | 디코드 지연 안 해치고 프리필 처리량만 조정 |
| `-c, --ctx-size N` | 컨텍스트 축소 = KV 메모리↓·비용↓ | 너무 작으면 히스토리 잘림 |
| `-b/-ub, --batch/ubatch` | 프리필 처리량↑ | 메모리·스텝당 연산↑ |
| `-fa, --flash-attn on` | 어텐션 가속 + KV 메모리↓ | 일부 구형/CPU 미지원·느릴 수 있음 |
| `--mlock` | 가중치를 RAM에 상주(스왑 방지) | 그만큼 물리 RAM 점유 |
| `--no-mmap` | 시작 시 전체 로드 | 초기 로딩 느리나 추론 중 페이지폴트↓ |
| `-ctk/-ctv, --cache-type-k/v` | KV 양자화 | -fa 필요, 품질/속도 소폭 트레이드 |
| `--cache-prompt` (기본 on) | 공유 프리픽스 KV 재사용 → **TTFT↓** | — |
| `--cache-reuse N` | 프리픽스가 아니어도 N토큰 이상 청크를 KV-shift로 재사용 | 프롬프트 일부만 바뀔 때 프리필↓ |
| `--slot-save-path` + `/slots` API | 슬롯 KV를 디스크에 저장/복원 → 세션 간 콜드 프리필 제거 | 디스크 I/O |
| `-np, --parallel N` / `-cb`(기본 on) | 연속 배칭·다중 슬롯 | 슬롯마다 KV 영역 필요 → 메모리 N배, 부하 시 지연↑ |
| `-md, --model-draft` + `--draft-max N` | 스페큘러티브 디코딩 | draft 정확하면 생성 20~50%↑, 안 맞으면 되레 느려짐 |
| `-ngld / -ctkd/-ctvd` | draft 모델 별도 오프로드·KV 양자화 | draft가 본 모델 VRAM/지연 안 잠식 |
| `--keep N` | 컨텍스트 넘칠 때 초기 N토큰(시스템프롬프트) 보존 | 캐시된 시스템프롬프트 재프리필 방지 |
| `--warmup` (기본 on) | 빈 패스로 모델 예열 | 첫 요청 콜드스타트↓ |

- **프롬프트 캐싱 관련 정정:** llama-server에는 독립 `--prompt-cache` 플래그가 **없다.** 등가물은 `--cache-prompt`(토글) + `--slot-save-path`/`/slots`(디스크 지속). `--prompt-cache FILE`은 구형 `llama-cli`에만 존재. ([server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md))
- **서버 상주가 핵심:** llama-server를 상시 프로세스로 띄워 모델을 메모리에 유지하면 매 호출마다의 수 초짜리 GGUF 재로딩(콜드스타트)을 없앤다. ([server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md))
- 스페큘러티브 디코딩은 **만능이 아니다** — 2026 RTX3090 Qwen3 A3B(MoE) 벤치에서 어떤 draft 조합도 순이득 없음(이득은 수용률·본모델 속도에 좌우). ([qwen3 spec bench](https://github.com/thc1006/qwen3.6-speculative-decoding-rtx3090)) 반면 CPU-only 3B에서 12.9→22.1 tok/s(1.72배) 사례도 있음. ([DataCamp](https://www.datacamp.com/tutorial/multi-token-prediction-llama-cpp))

---

## 3. 프롬프트·컨텍스트 최적화 (반복 프리필 비용 제거)

프리필은 프롬프트 길이에 O(n)(어텐션은 O(n²))이라 **주입 컨텍스트를 줄이는 것 자체가 직접적인 TTFT 지렛대.**

- **프리픽스/프롬프트 캐싱:** 고정 시스템프롬프트·긴 공유 프리픽스의 KV를 캐시해 재프리필을 건너뜀. llama-server는 유휴 슬롯 KV를 호스트 RAM에 저장(`--cache-ram`/`--cram`, 기본 ~8GiB, 2025-10부터 기본 활성)하고 프리픽스 일치 시 복원. 반복 요청이 전체 프롬프트 대신 **1토큰만** 처리되는 로그로 확인됨. ([llama.cpp #8947](https://github.com/ggml-org/llama.cpp/discussions/8947), [#13606](https://github.com/ggml-org/llama.cpp/discussions/13606))
- 일반 원리로 프리픽스 캐싱은 긴 공유 시스템프롬프트(2K+)에서 **TTFT를 약 60~80% 절감**(한 에이전트 사례 480ms→110ms). SGLang RadixAttention은 최대 5배 처리량·큰 TTFT 감소. ([SqueezeBits](https://blog.squeezebits.com/vllm-vs-tensorrtllm-12-automatic-prefix-caching-38189), [LMSYS SGLang](https://www.lmsys.org/blog/2024-01-17-sglang/))
- **프롬프트를 짧게:** RAG는 흔히 쿼리당 4K~16K 토큰을 프리필한다. 리랭킹으로 청크 수를 줄이고 관련도 기반 압축을 하면 입력 토큰을 20~40% 절감, 한 사례는 프리필 10초→2초. ([Unstructured](https://unstructured.io/insights/retrieval-latency-optimization-for-production-rag-systems), [dasroot](https://dasroot.net/posts/2026/05/prefill-bottleneck-token-generation-latency-prompt-processing/))
- **컨텍스트 시프팅(StreamingLLM):** 초기 "어텐션 싱크" 토큰 몇 개(4개면 충분) + 최근 창을 유지해, 컨텍스트가 넘칠 때 전체 재프리필 없이 KV를 밀어냄 → 최대 22.2배 속도. llama.cpp 컨텍스트 시프팅의 기반. **단, Gemma 2/3 같은 SWA 모델은 `--swa-full`을 켜야 캐시 재사용이 동작.** ([arXiv 2309.17453](https://arxiv.org/abs/2309.17453), [llama.cpp #13606](https://github.com/ggml-org/llama.cpp/discussions/13606))
- **출력 토큰 상한 + 스트리밍:** 디코드가 대개 지배적 지연이라 `max_tokens` 축소·정지시퀀스로 출력 50% 줄이면 지연도 ~50%↓. 스트리밍은 총 시간이 같아도 첫 토큰이 빠르면 체감이 훨씬 빠름. ([OpenAI latency 가이드](https://developers.openai.com/api/docs/guides/latency-optimization), [Redis](https://redis.io/blog/llm-token-optimization-speed-up-apps/))

---

## 4. 모델 아키텍처 선택 — 같은 "크기"라도 지연이 다르다

- **MoE / effective-param 모델:** 토큰마다 일부 전문가만 활성 → 총 용량은 크되 활성 파라미터는 작아 밀집(dense) 모델보다 지연이 낮다. 예: 25.2B 총 파라미터 MoE가 토큰당 ~3.8B만 활성 → dense 2~4B급 지연. ([Qubrid](https://www.qubrid.com/blog/google-gemma-4-technical-deep-dive-architecture-moe-benchmarks-production-guide))
- **Gemma 3n(MatFormer):** E4B 안에 완전 동작하는 E2B가 중첩 → E2B만 뽑아 쓰면 최대 **2배 빠른 추론.** E2B/E4B는 원시 5B/8B이지만 Per-Layer Embedding으로 **2GB/3GB RAM**의 2B/4B급 메모리로 동작(E2B 실효 ~1.91B). KV 캐시 공유로 **긴 입력 TTFT를 Gemma3 4B 대비 2배 개선.** ([Google 개발자 블로그](https://developers.googleblog.com/en/introducing-gemma-3n-developer-guide/), [Gemma 3n 문서](https://ai.google.dev/gemma/docs/gemma-3n))
- **GQA(Grouped-Query Attention):** 여러 쿼리헤드가 K/V 헤드를 공유해 KV 캐시·어텐션 대역폭을 최대 ~8배 축소 → 디코드가 대역폭 바운드이므로 그만큼 생성/긴컨텍스트가 빨라짐. ([zeroentropy](https://zeroentropy.dev/concepts/grouped-query-attention/))
- **증류(Distillation):** 작은 학생 모델로 ~5~50배 지연↓(DistilGPT-2는 35~40% 작고 1.5배 빠르며 성능 95~97% 유지). ([Distil Labs](https://www.distillabs.ai/learn/knowledge-distillation-for-llms/))
- **가장 확실한 규칙:** 품질 기준을 만족하는 **가장 작은 모델(대개 2~4B)** 을 골라라.

---

## 5. 하드웨어 백엔드

- llama.cpp는 CPU 기능을 자동 감지해 AVX/AVX2/AVX512·ARM NEON 커널 사용 → Python 프레임워크 대비 대략 3~8배 빠른 CPU 추론. ([llama-cpp.com](https://llama-cpp.com/))
- **OpenBLAS/BLAS는 프리필만 가속하고 생성은 못 올린다** → 긴 프롬프트에서만 이득. ([Qwen 문서](https://qwen.readthedocs.io/en/latest/run_locally/llama.cpp.html))
- **부분 GPU 오프로드도 도움:** `-ngl`을 올릴수록 더 많은 레이어가 GPU로(999=완전 오프로드), VRAM이 빠듯하면 낮은 값으로 일부만 올려도 디코드가 빨라짐. ([knightli](https://knightli.com/en/2026/05/09/llama-cpp-multi-gpu-offload-performance/))
- 백엔드 성숙도/이식성: **CUDA**가 NVIDIA에서 가장 빠르고 성숙. **Vulkan**은 크로스벤더 이식성이 좋지만 NVIDIA에서 CUDA보다 느리고 AMD RDNA3/Intel Arc에서 CUDA의 ~70~85%. Apple은 **Metal**, Intel GPU는 **SYCL**, Qualcomm Adreno는 **OpenCL** 권장. ([llama.cpp #10879](https://github.com/ggml-org/llama.cpp/discussions/10879), [Qualcomm](https://www.qualcomm.com/developer/blog/2024/11/introducing-new-opn-cl-gpu-backend-llama-cpp-for-qualcomm-adreno-gpu))

---

## 6. LevA 앱에 대한 실전 권장 (적용 체크리스트)

이 앱의 구조(llama-server 상주, 모델 스왑, RAG 컨텍스트 주입, Gemma 4 E4B + MiniCPM-V)에 맞춘 우선순위.

**즉시 효과 큰 것 (구현 난이도 낮음)**
1. **서버 상주 유지 + 모델 스왑 최소화.** 지금은 text↔vision 전환마다 서버를 재기동(콜드스타트)한다. 대화·문서캐싱을 같은 text 모델로 몰고, 비전은 배치로 모아 한 번만 로드하면 재기동 횟수가 급감. 상주 자체가 GGUF 재로딩 수 초를 없앰.
2. **프롬프트 캐싱 활용.** `--cache-prompt`(기본 on)에 더해, 대화 시 **고정 지시문(시스템프롬프트)을 프롬프트 맨 앞에 두면** 프리픽스 KV가 재사용돼 TTFT가 60~80% 준다. 매 요청 앞부분을 동일하게 유지하는 게 핵심.
3. **주입 컨텍스트를 짧게.** 채팅 RAG에서 파일 본문을 최대 7,000자 넣는데, **상위 1~2개·관련 구간만** 넣고 리랭크/압축하면 프리필이 크게 줄어든다(프리필은 길이에 비례).
4. **출력 상한 + 스트리밍(이미 적용).** `max_tokens`를 과하지 않게, 스트리밍으로 체감 지연↓.

**설정 권장값 (프로필별)**
- **CPU-only PC:** `-t <물리코어수>`, 양자화 **Q4_K_M**(또는 CPU 생성 중시 Q4_0 + 온라인 리팩), `-c`는 실제 필요한 만큼만(예: 4096~8192), `--mlock`으로 상주, `-fa on`(지원 시). i-quant는 피함.
- **소형/내장 GPU:** `-ngl`을 VRAM 한도까지 최대한(부분 오프로드도 이득), `-fa on`, VRAM 여유 시 Q5_K_M. 긴 컨텍스트로 KV가 부족하면 `-ctk q8_0 -ctv q8_0`(-fa 필수).
- **긴 문서/컨텍스트 시:** 컨텍스트 시프팅 활용, Gemma류 SWA면 `--swa-full`, `--keep`으로 시스템프롬프트 보존.

**중기 개선 (효과 크나 작업량 있음)**
5. **모델 선택:** Gemma 4 E4B는 이미 effective-param(MoE류) 계열이라 유리. 더 낮은 지연이 필요하면 **더 작은 변형(2B급)** 또는 Gemma 3n E2B로 낮추는 옵션 제공.
6. **스페큘러티브 디코딩:** 작은 draft 모델(`-md`)로 생성 20~50%↑ 가능하나 **모델 궁합에 따라 되레 느려질 수 있으니** 실측 후 채택. MoE 타깃에선 이득이 안 날 수 있음.
7. **KV 캐시 재사용(`--cache-reuse`)**: 대화가 이어질 때 바뀐 부분만 프리필.

---

## 부록: 확인된 주의사항(검증 과정에서 걸러낸 것)

- llama-server에 `--prompt-cache`, `--debug-slot`, `--slot-id-N` 같은 플래그는 **없음**(일부 블로그의 "93% TTFT 감소" 등은 존재하지 않는 플래그를 인용한 AI 생성 오정보로 확인됨). 실제 등가물은 `--cache-prompt` + `--slot-save-path`/`/slots`. ([검증: 리서치 에이전트가 llama.cpp #20574를 배제])
- i-quant는 **Vulkan 비호환·CPU/Metal에서 느림** → 백엔드 확인 후 선택.
- KV 양자화는 **속도가 아니라 메모리 절감**이 주목적(q4_0은 긴 컨텍스트에서 생성이 느려질 수 있음).
- 스페큘러티브 디코딩은 **항상 이득이 아님**(수용률 의존).

---

### 주요 출처
- 프리필/디코드 원리: [plienhar](https://medium.com/@plienhar/llm-inference-series-5-dissecting-model-performance-6144aa93168f), [Towards Data Science](https://towardsdatascience.com/prefill-is-compute-bound-decode-is-memory-bound-why-your-gpu-shouldnt-do-both/), [finbarr.ca](https://finbarr.ca/how-is-llama-cpp-possible/), [Weka](https://www.weka.io/learn/ai-ml/prefill-and-decode/), [Arm](https://learn.arm.com/learning-paths/servers-and-cloud-computing/llama_cpp_streamline/2_llama.cpp_intro/)
- 양자화: [llama.cpp quantize README](https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md), [Artefact2 KLD](https://gist.github.com/Artefact2/b5f810600771265fc1e39442288e8ec9), [bartowski 카드](https://huggingface.co/bartowski/Llama-3.3-70B-Instruct-GGUF), [PR #9921](https://github.com/ggerganov/llama.cpp/pull/9921)
- KV 캐시 양자화: [#5932](https://github.com/ggml-org/llama.cpp/discussions/5932), [smcleod](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/), [NVIDIA 포럼](https://forums.developer.nvidia.com/t/kv-cache-quantization-benchmarks-on-dgx-spark-q4-0-vs-q8-0-vs-f16-llama-cpp-nemotron-30b-128k-context/365138)
- 서버 플래그: [tools/server/README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [speculative.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
- 프롬프트 캐싱: [#8947](https://github.com/ggml-org/llama.cpp/discussions/8947), [#13606](https://github.com/ggml-org/llama.cpp/discussions/13606), [SqueezeBits](https://blog.squeezebits.com/vllm-vs-tensorrtllm-12-automatic-prefix-caching-38189), [LMSYS SGLang](https://www.lmsys.org/blog/2024-01-17-sglang/), [StreamingLLM](https://arxiv.org/abs/2309.17453)
- 아키텍처: [Gemma 3n 개발자 블로그](https://developers.googleblog.com/en/introducing-gemma-3n-developer-guide/), [Gemma 3n 문서](https://ai.google.dev/gemma/docs/gemma-3n), [GQA](https://zeroentropy.dev/concepts/grouped-query-attention/), [증류](https://www.distillabs.ai/learn/knowledge-distillation-for-llms/)
- 백엔드: [llama-cpp.com](https://llama-cpp.com/), [Qwen 문서](https://qwen.readthedocs.io/en/latest/run_locally/llama.cpp.html), [#10879](https://github.com/ggml-org/llama.cpp/discussions/10879), [Qualcomm OpenCL](https://www.qualcomm.com/developer/blog/2024/11/introducing-new-opn-cl-gpu-backend-llama-cpp-for-qualcomm-adreno-gpu)
