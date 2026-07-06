"""Golden-set evaluation harness for the RAG engine (ARCHITECTURE.md §8).

Measures the metrics that gate a release:
  - groundedness:      answer cites at least one valid source when it should
  - citation_accuracy: no invalid/hallucinated [Sn] labels
  - refusal_correctness: refuses when the answer isn't in the corpus
  - answer_relevance:  cheap lexical overlap with expected key terms

Runs against any provider (real Ollama or a stub), so it works in CI without
models by injecting a scripted provider. Returns a 0-100 RAG quality score
feeding the review gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from ..config import Settings
from .engine import RagEngine, Answer


@dataclass
class GoldenCase:
    question: str
    expected_terms: List[str] = field(default_factory=list)
    should_refuse: bool = False


@dataclass
class EvalResult:
    total: int
    groundedness: float
    citation_accuracy: float
    refusal_correctness: float
    answer_relevance: float
    rag_score: float           # weighted 0-100
    details: list = field(default_factory=list)


def _relevance(answer: str, terms: List[str]) -> float:
    if not terms:
        return 1.0
    a = answer.lower()
    hit = sum(1 for t in terms if t.lower() in a)
    return hit / len(terms)


def evaluate(engine: RagEngine, cases: List[GoldenCase]) -> EvalResult:
    n = len(cases) or 1
    g = c = r = rel = 0.0
    details = []

    for case in cases:
        ans: Answer = engine.query(case.question)
        if case.should_refuse:
            ok_refuse = 1.0 if ans.refused else 0.0
            r += ok_refuse
            # a correct refusal is fully grounded + no bad citations
            g += ok_refuse
            c += 1.0 if not any("존재하지 않는 인용" in w for w in ans.warnings) else 0.0
            rel += ok_refuse
            details.append({"q": case.question, "refused": ans.refused,
                            "expected_refuse": True, "pass": bool(ok_refuse)})
        else:
            grounded = 1.0 if (ans.grounded and ans.citations and not ans.refused) else 0.0
            no_bad_cite = 0.0 if any("존재하지 않는 인용" in w for w in ans.warnings) else 1.0
            not_wrong_refuse = 1.0 if not ans.refused else 0.0
            relevance = _relevance(ans.text, case.expected_terms)
            g += grounded
            c += no_bad_cite
            r += not_wrong_refuse
            rel += relevance
            details.append({"q": case.question, "grounded": bool(grounded),
                            "citations": len(ans.citations),
                            "relevance": round(relevance, 2),
                            "pass": bool(grounded and no_bad_cite)})

    groundedness = g / n
    citation_accuracy = c / n
    refusal_correctness = r / n
    answer_relevance = rel / n
    # weights within RAG-quality (mirror §8 emphasis)
    rag_score = 100 * (0.35 * groundedness + 0.30 * citation_accuracy +
                       0.20 * refusal_correctness + 0.15 * answer_relevance)
    return EvalResult(
        total=len(cases), groundedness=round(groundedness, 3),
        citation_accuracy=round(citation_accuracy, 3),
        refusal_correctness=round(refusal_correctness, 3),
        answer_relevance=round(answer_relevance, 3),
        rag_score=round(rag_score, 1), details=details,
    )


def load_cases(path: Path) -> List[GoldenCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [GoldenCase(**c) for c in raw]


if __name__ == "__main__":
    import sys
    print("Use via tests or: evaluate(engine, load_cases('golden.json'))")
