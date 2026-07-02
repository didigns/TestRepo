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
    # 캐시 DB
    "dbPath": "",           # 비우면 사용자 데이터 경로에 leva-cache.db
    # PDF 처리
    "pdfMaxPages": 5,       # 비전 처리 시 렌더링할 최대 페이지
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
