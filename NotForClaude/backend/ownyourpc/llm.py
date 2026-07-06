"""LLM + embedding provider over Ollama (local HTTP backend).

One of two interchangeable backends (see providers.make_provider). Kept as
a thin seam so the RAG/meeting code never talks to a specific runtime.
All calls hit 127.0.0.1 — nothing leaves the machine.
"""
from __future__ import annotations

from typing import Iterator, Optional

import requests

from .config import Settings


class OllamaError(RuntimeError):
    """Backend call failed — carries the server's error body for debugging."""


def _check(r: requests.Response, what: str) -> requests.Response:
    """raise_for_status but keep the server's JSON error message."""
    if not r.ok:
        detail = ""
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = (r.text or "")[:400]
        raise OllamaError(f"{what} 실패 (HTTP {r.status_code}): {detail}")
    return r


class OllamaProvider:
    def __init__(self, settings: Settings):
        self.host = settings.ollama_host.rstrip("/")
        self.settings = settings

    # ---- availability -----------------------------------------------
    def is_up(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            return r.ok
        except requests.RequestException:
            return False

    def version(self) -> str:
        try:
            r = requests.get(f"{self.host}/api/version", timeout=3)
            return r.json().get("version", "unknown") if r.ok else "unknown"
        except requests.RequestException:
            return "unknown"

    def installed_models(self) -> list:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except requests.RequestException:
            return []

    # ---- embeddings -------------------------------------------------
    def embed(self, text: str, model: Optional[str] = None) -> list:
        model = model or self.settings.profile().embed_model
        r = requests.post(f"{self.host}/api/embeddings",
                          json={"model": model, "prompt": text}, timeout=120)
        _check(r, f"임베딩({model})")
        return r.json()["embedding"]

    def embed_batch(self, texts: list, model: Optional[str] = None) -> list:
        return [self.embed(t, model) for t in texts]

    # ---- generation -------------------------------------------------
    FALLBACK = {"gemma4:e4b": "gemma4:e2b"}

    def _generate_once(self, prompt, system, model, temp) -> str:
        r = requests.post(
            f"{self.host}/api/generate",
            json={"model": model, "prompt": prompt, "system": system,
                  "stream": False, "options": {"temperature": temp}},
            timeout=600)
        _check(r, f"생성({model})")
        return r.json().get("response", "").strip()

    def generate(self, prompt: str, system: str = "", model: Optional[str] = None,
                 temperature: Optional[float] = None,
                 allow_fallback: bool = True) -> str:
        model = model or self.settings.profile().llm_model
        temp = self.settings.temperature if temperature is None else temperature
        try:
            return self._generate_once(prompt, system, model, temp)
        except OllamaError:
            fb = self.FALLBACK.get(model)
            installed = self.installed_models()
            if allow_fallback and fb and any(fb.split(":")[0] in m for m in installed):
                return self._generate_once(prompt, system, fb, temp)
            raise

    def generate_stream(self, prompt: str, system: str = "",
                        model: Optional[str] = None) -> Iterator[str]:
        import json
        model = model or self.settings.profile().llm_model
        with requests.post(
            f"{self.host}/api/generate",
            json={"model": model, "prompt": prompt, "system": system,
                  "stream": True, "options": {"temperature": self.settings.temperature}},
            stream=True, timeout=600,
        ) as r:
            _check(r, f"생성 스트림({model})")
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("response"):
                    yield chunk["response"]
                if chunk.get("done"):
                    break
