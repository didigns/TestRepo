"""End-to-end smoke test — run ON YOUR MACHINE (needs Ollama + models).

Creates a tiny sample corpus, ingests it with the real embedding model,
asks grounded questions against real gemma4:e4b, and runs the golden-set
eval to produce a REAL RAG quality score (the M1 review gate).

    cd backend
    python scripts/smoke_test.py

Exits 0 if the review gate passes (RAG score >= 85), else non-zero.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisummary.config import load_settings
from aisummary.providers import make_provider
from aisummary.rag.vectorstore import VectorStore
from aisummary.rag.engine import RagEngine
from aisummary.ingest.watcher import Ingestor
from aisummary.rag.evaluate import evaluate, GoldenCase

SAMPLE_DOC = """\
서비스 계약서 (요약)

제3조 (계약 기간)
본 계약의 유효 기간은 2026년 1월 1일부터 2026년 12월 31일까지로 한다.
계약 만료일은 2026년 12월 31일이다.

제7조 (위약금)
당사자 일방이 계약을 위반하여 해지하는 경우, 위약금으로 계약 총액의
10%를 상대방에게 지급한다.

제9조 (비밀유지)
양 당사자는 계약 기간 및 종료 후 2년간 비밀정보를 유지한다.
"""

CASES = [
    GoldenCase(question="계약 만료일은 언제인가요?",
               expected_terms=["2026", "12", "31"], should_refuse=False),
    GoldenCase(question="위약금은 얼마인가요?",
               expected_terms=["10", "%"], should_refuse=False),
    GoldenCase(question="화성 기지 건설 예산은 얼마로 책정되어 있나요?",
               expected_terms=[], should_refuse=True),
]


def main() -> int:
    s = load_settings()
    prov = make_provider(s)

    print(f"1) 백엔드 점검 ({s.backend})...")
    if not prov.is_up():
        print("   ✗ 백엔드 미준비. llamacpp면 'pip install llama-cpp-python', "
              "ollama면 'ollama serve'.")
        return 1
    if s.backend == "llamacpp" and not prov.installed_models():
        print("   GGUF 모델 다운로드 중 (최초 1회)...")
        prov.ensure_models()
    print(f"   ✓ 준비됨. 모델: {prov.installed_models()}")

    with tempfile.TemporaryDirectory() as td:
        doc = Path(td) / "contract.txt"
        doc.write_text(SAMPLE_DOC, encoding="utf-8")

        print("2) 인제스천 (실제 임베딩 모델)...")
        store = VectorStore(s)
        ing = Ingestor(s, store, prov,
                       on_event=lambda ev, m: print(f"   [{ev}] {m}"))
        res = ing.scan_folder(Path(td))
        print(f"   ✓ {res}")

        print("3) 실제 질의 (gemma4)...")
        eng = RagEngine(s, store, prov)
        a = eng.query("계약 만료일은 언제인가요?")
        print(f"   답변: {a.text}")
        print(f"   근거 {len(a.citations)}개, 신뢰도 {a.confidence:.2f}")

        print("4) 골든셋 실측 평가...")
        result = evaluate(eng, CASES)
        print(f"   groundedness={result.groundedness} "
              f"citation={result.citation_accuracy} "
              f"refusal={result.refusal_correctness} "
              f"relevance={result.answer_relevance}")
        print(f"   >>> RAG SCORE = {result.rag_score}/100 "
              f"({'PASS ✓' if result.rag_score >= 85 else 'FAIL ✗ 재작업'})")

        # cleanup this test's chunks so we don't pollute the real index
        store.delete_source(str(doc))
        return 0 if result.rag_score >= 85 else 2


if __name__ == "__main__":
    raise SystemExit(main())
