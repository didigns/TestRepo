"""캐싱 에이전트 설정. Electron 설정(JSON)과 공유한다."""
import json
import os

DEFAULT_CONFIG = {
    # llama.cpp
    "llamaServerExe": r"C:\Work\llamaCpp\llama-server.exe",
    "host": "127.0.0.1",
    "port": 8080,
    # 모델(gguf) 경로 - 사용자가 설정에서 지정
    "textModel": "",        # Gemma 4b gguf (그 외 파일)
    "visionModel": "",      # MiniCPM-V 4.6 gguf (pdf/이미지)
    "visionMmproj": "",     # MiniCPM-V mmproj gguf (비전 투영기)
    # 서버 파라미터
    "nCtx": 8192,
    "nGpuLayers": 0,        # GPU 오프로딩 레이어 수(0=CPU)
    "startupTimeout": 120,  # 서버 기동 대기(초)
    # 레이턴시 최적화 파라미터
    "nThreads": 0,          # 생성 스레드 수(0=llama.cpp 자동, 권장: 물리 코어 수)
    "flashAttn": "auto",    # Flash Attention: on|off|auto (어텐션 가속·KV 메모리↓)
    "useMlock": True,       # 모델을 RAM에 상주시켜 페이지폴트/스왑 방지
    # 캐시 DB
    "dbPath": "",           # 비우면 사용자 데이터 경로에 leva-cache.db
    # PDF 처리
    "pdfMaxPages": 5,       # 비전 처리 시 렌더링할 최대 페이지
    # 반복(loop) 추출: 본문을 chunkChars 자로 잘라 최대 maxChunks 조각 순차 처리
    "chunkChars": 2000,
    "maxChunks": 12,
    # 의미검색(임베딩): 비우면 토큰검색 사용
    "embedModel": "",       # 임베딩 모델 gguf (예: bge-m3)
    "embedPort": 8081,      # 임베딩 전용 서버 포트(메인과 분리)
    "embedNCtx": 2048,
    "embedPooling": "mean",
    "embedMinScore": 0.25,  # 코사인 최소 유사도
    # 청크 임베딩(검색 정확도용, 분석용보다 촘촘하게)
    "embedChunkChars": 1000,
    "embedMaxChunks": 0,      # 청크 개수 제한(0=무제한, 본문 전체 임베딩)
    "embedBatch": 64,         # 임베딩 요청 1건당 청크 수(무제한 청크 대비 배치 처리)
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
