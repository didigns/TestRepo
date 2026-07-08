"""Backend factory. Returns the configured local model provider.

    settings.backend == "llamacpp"  -> in-process GGUF (default, no daemon)
    settings.backend == "ollama"    -> Ollama HTTP (127.0.0.1)

Both providers share the same interface, so engine/ingest/meetings code is
backend-agnostic.
"""
from __future__ import annotations

from .config import Settings
from .llm import OllamaProvider, OllamaError  # noqa: F401 (re-export)


def make_provider(settings: Settings):
    if settings.backend == "ollama":
        return OllamaProvider(settings)
    from .llm_llamacpp import LlamaCppProvider
    return LlamaCppProvider(settings)
