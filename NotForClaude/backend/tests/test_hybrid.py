"""Hybrid retrieval tests — BM25, RRF, tokenizer, engine integration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisummary.config import Settings
from aisummary.rag.hybrid import tokenize, BM25, reciprocal_rank_fusion
from aisummary.rag.engine import RagEngine
from test_core import _store_with, FakeProvider


def test_tokenize_hangul_bigrams():
    toks = tokenize("계약 만료일")
    assert "계약" in toks
    assert "만료" in toks and "료일" in toks   # bigrams
    assert "abc" in tokenize("ABC 문서")        # latin lowercased


def test_bm25_ranks_relevant_doc_first():
    corpus = [tokenize(t) for t in [
        "계약 만료일은 2026년 12월 31일이다",
        "점심 메뉴는 김치찌개",
        "위약금은 계약 총액의 10%",
    ]]
    bm = BM25(corpus)
    top = bm.top("계약 만료일", n=3)
    assert top and top[0][0] == 0            # doc 0 is most relevant


def test_rrf_fuses_ranks():
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a"]])
    assert fused["a"] > fused["c"]           # a appears high in both
    assert set(fused) == {"a", "b", "c"}


def test_engine_hybrid_retrieval_runs():
    s = Settings(tier="low", similarity_threshold=0.0, hybrid_search=True)
    store = _store_with(s, ["계약 만료일은 2026년 12월 31일이다",
                            "위약금은 계약 총액의 10%",
                            "무관한 바나나 텍스트"])
    prov = FakeProvider(s, reply="답변 [S1].")
    eng = RagEngine(s, store, prov)
    ranked, best = eng._retrieve("계약 만료일")
    assert len(ranked) >= 1
    # BM25 should surface the contract-expiry chunk
    assert any("만료일" in r.text for r in ranked)


def test_engine_hybrid_vs_vector_toggle():
    for hybrid in (True, False):
        s = Settings(tier="low", similarity_threshold=0.0, hybrid_search=hybrid)
        store = _store_with(s, ["계약 만료일 2026", "위약금 10%"])
        eng = RagEngine(s, store, FakeProvider(s, reply="x [S1]."))
        ranked, _ = eng._retrieve("위약금")
        assert len(ranked) >= 1
