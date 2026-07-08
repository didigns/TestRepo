"""In-process GGUF backend via llama-cpp-python.

No daemon, no separate server — the model runs inside this Python process,
which removes the whole class of Ollama service bugs and makes the app
self-contained (bundle the .gguf files). Implements the same interface as
OllamaProvider so the RAG/meeting code is backend-agnostic.

Heavy deps (llama_cpp, huggingface_hub) are imported lazily so the module
imports fine on a machine that hasn't installed them yet.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator, Optional

from .config import Settings, MODELS_CACHE
from .llm import OllamaError  # reuse the same error type


class LlamaCppProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm = None          # lazy Llama (chat)
        self._embed = None        # lazy Llama (embedding=True)
        self._llm_key = None      # which gguf is currently loaded
        # llama.cpp models are NOT thread-safe — serialize native calls so
        # concurrent requests (e.g. live-summary + title during a meeting)
        # can't segfault the process.
        self._gen_lock = threading.Lock()
        self._emb_lock = threading.Lock()

    # ---- model file resolution / download ---------------------------
    def _resolve(self, repo: str, filename: str) -> Optional[Path]:
        """Return local path to a GGUF, downloading if needed. None on failure."""
        MODELS_CACHE.mkdir(parents=True, exist_ok=True)
        local = MODELS_CACHE / filename
        if local.exists():
            return local
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise OllamaError(
                "huggingface_hub 미설치 → 'pip install -r requirements.txt' 를 먼저 실행하세요")
        try:
            got = hf_hub_download(repo_id=repo, filename=filename,
                                  local_dir=str(MODELS_CACHE))
            return Path(got)
        except Exception as e:  # network off / missing file
            raise OllamaError(f"GGUF 다운로드 실패 ({repo}/{filename}): {e}")

    def ensure_models(self, log=print) -> None:
        lrepo, lfile = self.settings.llm_gguf()
        erepo, efile, _ = self.settings.embed_gguf()
        for repo, f in ((lrepo, lfile), (erepo, efile)):
            log(f"확인/다운로드: {repo}/{f}")
            self._resolve(repo, f)

    # ---- availability -----------------------------------------------
    def is_up(self) -> bool:
        try:
            import llama_cpp  # noqa: F401
            return True
        except ImportError:
            return False

    def version(self) -> str:
        try:
            import llama_cpp
            return getattr(llama_cpp, "__version__", "unknown")
        except ImportError:
            return "not-installed"

    def installed_models(self) -> list[str]:
        if not MODELS_CACHE.exists():
            return []
        return [p.name for p in MODELS_CACHE.glob("*.gguf")]

    # ---- lazy loaders -----------------------------------------------
    def _get_llm(self, key: str, path: Path):
        if self._llm is not None and self._llm_key == key:
            return self._llm
        from llama_cpp import Llama
        n_ctx = min(self.settings.profile().context_tokens, 8192)
        self._llm = Llama(model_path=str(path), n_ctx=n_ctx, n_threads=None,
                          verbose=False)
        self._llm_key = key
        return self._llm

    def _get_embed(self, path: Path):
        if self._embed is not None:
            return self._embed
        from llama_cpp import Llama
        self._embed = Llama(model_path=str(path), embedding=True, verbose=False)
        return self._embed

    # ---- embeddings -------------------------------------------------
    def embed(self, text: str, model: Optional[str] = None) -> list[float]:
        erepo, efile, _ = self.settings.embed_gguf()
        path = self._resolve(erepo, efile)
        with self._emb_lock:
            emb = self._get_embed(path)
            out = emb.create_embedding(text)
        vec = out["data"][0]["embedding"]
        # some builds return a list-of-lists (token embeddings) -> mean-pool
        if vec and isinstance(vec[0], list):
            n = len(vec)
            vec = [sum(col) / n for col in zip(*vec)]
        return vec

    def embed_batch(self, texts: list[str], model: Optional[str] = None) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    # ---- generation -------------------------------------------------
    def _messages(self, prompt: str, system: str):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return msgs

    def generate(self, prompt: str, system: str = "", model: Optional[str] = None,
                 temperature: Optional[float] = None,
                 allow_fallback: bool = True) -> str:
        temp = self.settings.temperature if temperature is None else temperature
        with self._gen_lock:                 # serialize native model access
            try:
                lrepo, lfile = self.settings.llm_gguf()
                path = self._resolve(lrepo, lfile)
                llm = self._get_llm(lfile, path)
                out = llm.create_chat_completion(
                    messages=self._messages(prompt, system), temperature=temp)
                return out["choices"][0]["message"]["content"].strip()
            except OllamaError:
                if allow_fallback and self.settings.tier != "low":
                    from .config import LLAMACPP_LLM
                    repo, f = LLAMACPP_LLM["low"]
                    path = self._resolve(repo, f)
                    llm = self._get_llm(f, path)
                    out = llm.create_chat_completion(
                        messages=self._messages(prompt, system), temperature=temp)
                    return out["choices"][0]["message"]["content"].strip()
                raise

    def generate_stream(self, prompt: str, system: str = "",
                        model: Optional[str] = None) -> Iterator[str]:
        lrepo, lfile = self.settings.llm_gguf()
        path = self._resolve(lrepo, lfile)
        with self._gen_lock:                 # serialize native model access
            llm = self._get_llm(lfile, path)
            for chunk in llm.create_chat_completion(
                    messages=self._messages(prompt, system),
                    temperature=self.settings.temperature, stream=True):
                delta = chunk["choices"][0].get("delta", {}).get("content")
                if delta:
                    yield delta
