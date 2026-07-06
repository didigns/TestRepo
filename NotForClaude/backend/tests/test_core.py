"""Core unit tests — run without Ollama or native deps (NumPy fallback store).

    cd backend && pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ownyourpc.config import Settings, TIERS
from ownyourpc.hardware import HardwareInfo, GPUInfo, select_tier
from ownyourpc.ingest.chunker import chunk_blocks
from ownyourpc.rag.vectorstore import VectorStore
from ownyourpc.rag.engine import RagEngine, REFUSAL


# ---- hardware tier selection ----------------------------------------
def _hw(ram, vram, vendor="none"):
    return HardwareInfo(os="Linux", arch="x86_64", cpu_model="test",
                        physical_cores=4, logical_cores=8, ram_gb=ram,
                        gpu=GPUInfo(vendor=vendor, name="g", vram_gb=vram))


def test_tier_low():
    assert select_tier(_hw(8, 0)).name == "low"


def test_tier_mid():
    assert select_tier(_hw(16, 0)).name == "mid"


def test_tier_high_by_vram():
    assert select_tier(_hw(16, 8, "nvidia")).name == "high"


def test_tier_high_by_ram():
    assert select_tier(_hw(32, 0)).name == "high"


# ---- chunker ---------------------------------------------------------
def test_chunker_preserves_metadata(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world " * 200, encoding="utf-8")
    blocks = [(1, f.read_text())]
    chunks = chunk_blocks(f, blocks, chunk_tokens=64, overlap_tokens=8)
    assert len(chunks) > 1
    assert all(c.source_path == str(f) for c in chunks)
    assert all(c.page == 1 for c in chunks)
    assert all(c.metadata["filename"] == "doc.txt" for c in chunks)


# ---- fake provider for engine tests ---------------------------------
class FakeProvider:
    """Deterministic embeddings: bag-of-chars vector. Scripted generation."""
    def __init__(self, settings, reply=""):
        self.settings = settings
        self.reply = reply

    def _vec(self, text):
        v = [0.0] * 8
        for ch in text.lower():
            if ch.isalpha():
                v[(ord(ch) - 97) % 8] += 1.0
        return v

    def embed(self, text, model=None):
        return self._vec(text)

    def embed_batch(self, texts, model=None):
        return [self._vec(t) for t in texts]

    def generate(self, prompt, system="", model=None, temperature=None):
        return self.reply


def _store_with(settings, texts):
    from ownyourpc.ingest.chunker import Chunk
    store = VectorStore.__new__(VectorStore)
    store.settings = settings
    store.dim = 8
    store._db = store._tbl = None
    from ownyourpc.rag.vectorstore import _NumpyStore
    store._fallback = _NumpyStore()
    prov = FakeProvider(settings)
    chunks, vecs = [], []
    for i, t in enumerate(texts):
        chunks.append(Chunk(id=f"c{i}", source_path=f"/f{i}.txt", page=1,
                            char_start=0, char_end=len(t), text=t,
                            metadata={"filename": f"f{i}.txt"}))
        vecs.append(prov._vec(t))
    store.add(chunks, vecs)
    return store


def test_engine_confidence_gate_refuses_on_empty():
    s = Settings(tier="low", similarity_threshold=0.9)
    store = _store_with(s, ["completely unrelated banana content"])
    prov = FakeProvider(s, reply="should not be used")
    eng = RagEngine(s, store, prov)
    ans = eng.query("quantum chromodynamics lagrangian")
    # high threshold -> refuse
    assert ans.refused is True
    assert ans.text == REFUSAL


def test_engine_grounded_answer_with_citation():
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, ["the contract expires on march third"])
    prov = FakeProvider(s, reply="계약 만료일은 3월 3일입니다 [S1].")
    eng = RagEngine(s, store, prov)
    ans = eng.query("contract expire date")
    assert ans.refused is False
    assert ans.grounded is True
    assert len(ans.citations) == 1
    assert ans.citations[0].label == "S1"


def test_engine_flags_invalid_citation():
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, ["the contract expires on march third"])
    prov = FakeProvider(s, reply="답 [S9].")  # S9 doesn't exist
    eng = RagEngine(s, store, prov)
    ans = eng.query("contract")
    assert any("존재하지 않는 인용" in w for w in ans.warnings)
    assert ans.grounded is False


def test_vectorstore_delete_source():
    s = Settings(tier="low")
    store = _store_with(s, ["alpha text here", "beta text here"])
    assert store.count() == 2
    store.delete_source("/f0.txt")
    assert store.count() == 1
