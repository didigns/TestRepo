"""Central configuration, paths, and tier definitions for OwnYourPC.

Everything is local-first. Data lives under ~/.ownyourpc by default.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# quiet noisy first-run HF download warnings (symlinks on Windows, no token)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

APP_NAME = "ownyourpc"
DATA_DIR = Path(os.environ.get("OWNYOURPC_HOME", Path.home() / f".{APP_NAME}"))
DB_DIR = DATA_DIR / "lancedb"
MODELS_CACHE = DATA_DIR / "models"
MEETINGS_DIR = DATA_DIR / "meetings"
CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class TierProfile:
    """A hardware tier maps to a concrete set of models + parameters."""
    name: str
    llm_model: str
    embed_model: str
    embed_dim: int
    stt_model: str
    enable_reranker: bool
    enable_diarization: bool
    context_tokens: int
    top_k: int
    chunk_tokens: int


# Tier table mirrors docs/ARCHITECTURE.md section 5.
TIERS: dict = {
    "low": TierProfile(
        name="low", llm_model="gemma4:e2b", embed_model="all-minilm",
        embed_dim=384, stt_model="tiny", enable_reranker=False,
        enable_diarization=False, context_tokens=8192, top_k=5, chunk_tokens=384),
    "mid": TierProfile(
        name="mid", llm_model="gemma4:e4b", embed_model="nomic-embed-text",
        embed_dim=768, stt_model="base", enable_reranker=False,
        enable_diarization=False, context_tokens=32768, top_k=8, chunk_tokens=512),
    "high": TierProfile(
        name="high", llm_model="gemma4:e4b", embed_model="qwen3-embedding:0.6b",
        embed_dim=1024, stt_model="small", enable_reranker=True,
        enable_diarization=True, context_tokens=131072, top_k=12, chunk_tokens=512),
}

# --- llama-cpp-python (in-process GGUF) backend specs ---------------
# Verified HuggingFace GGUF repositories.
LLAMACPP_LLM: dict = {
    "low":  ("unsloth/gemma-4-E2B-it-GGUF", "gemma-4-E2B-it-Q4_K_M.gguf"),
    "mid":  ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf"),
    "high": ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q6_K.gguf"),
}
# One embedding model for all tiers on the llamacpp backend (768-dim).
LLAMACPP_EMBED: tuple = (
    "nomic-ai/nomic-embed-text-v1.5-GGUF", "nomic-embed-text-v1.5.Q4_K_M.gguf", 768)


@dataclass
class Settings:
    """User-facing settings. Persisted to config.json; overridable in UI."""
    tier: str = "mid"
    backend: str = "llamacpp"             # "llamacpp" (in-process) | "ollama"
    stt_device: str = "cpu"               # "cpu" (default) | "cuda"
    stt_language: Optional[str] = "ko"    # None = auto-detect; "ko","en",...
    stt_model_override: Optional[str] = None  # None=>tier default; tiny/base/small/medium
    diarize_enabled: bool = False         # post-meeting speaker diarization (pyannote)
    hf_token: str = ""                    # HuggingFace token for gated pyannote model
    hybrid_search: bool = True            # BM25 + vector fusion (RRF)
    top_k_override: Optional[int] = None  # None => tier default
    watch_folders: list = field(default_factory=list)
    ollama_host: str = "http://127.0.0.1:11434"
    temperature: float = 0.2              # low = factual
    similarity_threshold: float = 0.25    # below this -> "not found"
    require_citations: bool = True
    telemetry: bool = False               # opt-in, default OFF

    def profile(self) -> TierProfile:
        return TIERS[self.tier]

    def llm_gguf(self) -> tuple:
        return LLAMACPP_LLM[self.tier]

    def embed_gguf(self) -> tuple:
        return LLAMACPP_EMBED

    def effective_embed_dim(self) -> int:
        """Embedding dim depends on backend (nomic=768 for llamacpp)."""
        if self.backend == "llamacpp":
            return LLAMACPP_EMBED[2]
        return self.profile().embed_dim


def ensure_dirs() -> None:
    for d in (DATA_DIR, DB_DIR, MODELS_CACHE, MEETINGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    ensure_dirs()
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items() if k in Settings().__dict__}
        return Settings(**known)
    return Settings()


def save_settings(s: Settings) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False),
                           encoding="utf-8")
