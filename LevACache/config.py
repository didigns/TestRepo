"""캐싱 에이전트 설정. Electron 설정(JSON)과 공유한다."""
import json
import os

DEFAULT_CONFIG = {
    # llama.cpp
    "llamaServerExe": r"C:\Work\llamaCpp\llama-server.exe",
    "host": "127.0.0.1",
    "port": 8080,
    # 모델(gguf) 경로 - 사용자가 설정에서 지정
    "textModel": "",        # Gemma 4 gguf (텍스트 채팅·요약)
    # 비전(이미지/스캔 PDF) 모델. 두 가지 구성이 가능하다:
    #  (A) 통합 멀티모달: visionModel 을 textModel 과 '같은 Gemma 4 gguf' 로 두고
    #      visionMmproj 에 Gemma 4용 mmproj 를 지정 → 스왑 없이 한 서버가 텍스트+
    #      이미지를 모두 처리(MiniCPM 불필요). 권장.
    #  (B) 분리형(레거시): visionModel=MiniCPM-V gguf, visionMmproj=MiniCPM mmproj →
    #      요청에 따라 text↔vision 서버를 재기동(스왑).
    "visionModel": "",      # (A) Gemma 4 gguf(=textModel)  또는  (B) MiniCPM-V gguf
    "visionMmproj": "",     # (A) Gemma 4 mmproj            또는  (B) MiniCPM mmproj
    # 서버 파라미터
    "nCtx": 8192,
    "nGpuLayers": 0,        # GPU 오프로딩 레이어 수(0=CPU)
    "startupTimeout": 120,  # 서버 기동 대기(초)
    # 레이턴시 최적화 파라미터
    "nThreads": 0,          # 생성 스레드 수(0=llama.cpp 자동, 권장: 물리 코어 수)
    "flashAttn": "auto",    # Flash Attention: on|off|auto (어텐션 가속·KV 메모리↓)
    "useMlock": True,       # 모델을 RAM에 상주시켜 페이지폴트/스왑 방지
    # ---- 저메모리 모드 ----
    # 켜면: (1) mlock 해제  (2) 유휴 시 모델 언로드(메인 포함)  (3) 임베딩 양자화
    #       모델 사용 권고 로그. 저사양(16GB급)에서 상주 메모리를 크게 줄인다
    #       (품질 손실 없음. 유휴 후 첫 요청은 재로딩으로 수 초 지연).
    "lowMemory": False,
    # 유휴 언로드 대기(초). 0=사용 안 함. lowMemory=True 이고 값이 0이면 300초 적용.
    # (lowMemory 없이 값만 >0 이면 보조 서버(embed/whisper)만 언로드, 메인은 유지)
    "idleUnloadSec": 0,
    # 캐시 DB
    "dbPath": "",           # 비우면 사용자 데이터 경로에 leva-cache.db
    # PDF 처리
    "pdfMaxPages": 0,       # 스캔 PDF OCR 페이지 수 (0=전체 페이지)
    # 반복(loop) 추출: 본문을 chunkChars 자로 잘라 최대 maxChunks 조각 순차 처리
    "chunkChars": 2000,
    "maxChunks": 12,
    # 의미검색(임베딩): 비우면 토큰검색 사용
    "embedModel": "",       # 임베딩 모델 gguf (예: bge-m3)
    "embedPort": 8081,      # 임베딩 전용 서버 포트(메인과 분리)
    "embedNCtx": 2048,
    # 풀링은 비워 두면 GGUF 메타데이터의 모델 기본값을 쓴다(BGE-M3 = CLS).
    # 과거 "mean" 강제는 CLS로 학습된 BGE-M3의 임베딩 공간을 붕괴시켜
    # (무관한 청크끼리 코사인 0.8+) 의미검색을 무력화했다.
    "embedPooling": "",
    "embedMinScore": 0.25,  # 코사인 최소 유사도
    # 청크 임베딩(검색 정확도용, 분석용보다 촘촘하게)
    "embedChunkChars": 1000,
    "embedMaxChunks": 0,      # 청크 개수 제한(0=무제한, 본문 전체 임베딩)
    "embedBatch": 64,         # 임베딩 요청 1건당 청크 수(무제한 청크 대비 배치 처리)
    # 음성(STT) — whisper.cpp whisper-server
    "whisperServerExe": "",   # whisper-server.exe 경로(자동 설치 시 채워짐)
    "whisperModel": "",       # ggml whisper 모델(.bin) 경로
    "whisperPort": 8082,      # STT 전용 서버 포트
    "whisperLanguage": "auto",  # 전사 언어(auto=자동 감지)
    "ffmpegExe": "",          # m4a 등 변환용 ffmpeg (비우면 PATH에서 탐색)
    # 실시간 자막(스트리밍) — faster-whisper 설치 시 자동 사용, 없으면 VAD 폴백
    "liveSttModel": "large-v3-turbo",
}


def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            # leva-config.json 은 cache.* 하위에 둘 수도 있음
            src = user.get("cache", user)
            for k in cfg:
                if k in src and src[k] not in (None, ""):
                    cfg[k] = src[k]
        except Exception:
            pass
    return cfg
