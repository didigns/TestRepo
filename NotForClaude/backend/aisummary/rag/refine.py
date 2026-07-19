"""Query refinement — rewrite a vague user question into a more specific,
keyword-rich search query for better retrieval.

Used for RETRIEVAL ONLY: the refined text drives embedding + BM25 search, while
the answer is still generated against the user's ORIGINAL question. That keeps
the reply faithful to what was actually asked and prevents the rewrite from
inventing scope the documents don't contain (the grounding/citation guardrail
downstream is unaffected).

Best-effort: any failure — model down, empty output — falls back to the original
question, so refinement can never break a query. The refine prompt is
conservative: it expands with synonyms/related terms but must not fabricate
constraints (dates, numbers, proper nouns) the user didn't give.
"""
from __future__ import annotations

import re

REFINE_SYSTEM = (
    "당신은 로컬 문서 검색을 위한 '질의 리라이터'입니다. 사용자의 (때로 짧고 모호한) 질문을 "
    "벡터·키워드 검색에 잘 걸리도록 '상세한 검색 질의' 한 줄로 확장해 다시 씁니다.\n"
    "규칙:\n"
    "1) 사용자가 실제로 묻는 바를 유지하세요. 없는 기간·수치·고유명사·조건을 지어내지 마세요.\n"
    "2) 핵심 의도에 더해 관련 하위 주제·동의어·상하위 개념·핵심 용어를 폭넓게 덧붙여 상세하게 만드세요.\n"
    "   (예: 'X가 뭐야' → 'X 개요, 주요 기능, 목적, 구성·작동 방식, 특징, 사용 사례' 처럼 검색에 유용한 측면을 나열)\n"
    "3) 질문이 이미 충분히 구체적이면 관련 용어만 약간 보강하세요.\n"
    "4) 설명·따옴표·머리말 없이 검색 질의문 '한 줄'만 출력하세요.\n"
    "5) 사용자의 언어로 출력하세요 (한국어 질문이면 한국어)."
)

# Leading labels a model might prepend despite rule 4 ("검색 질의: ...", "Query: ...").
_LABEL = re.compile(r"^\s*(검색\s*질의|질의|질문|refined\s*query|query)\s*[:：]\s*", re.I)


def _sanitize(raw: str, max_chars: int) -> str:
    """Reduce the model's output to a single clean search-query line."""
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    line = _LABEL.sub("", lines[0])
    line = line.strip().strip("\"'“”「」`").strip()
    return line[:max_chars].strip()


def refine_query(provider, question: str, *, temperature: float = 0.0,
                 max_chars: int = 400) -> str:
    """Return a search-optimized rewrite of `question`.

    `provider` is any object with `.generate(prompt, system=, temperature=)`
    (OllamaProvider / LlamaCppProvider). Falls back to the original question on
    empty input, a too-short/empty rewrite, or any provider error.
    """
    original = question or ""
    if not original.strip():
        return original
    try:
        raw = provider.generate(original.strip(), system=REFINE_SYSTEM,
                                temperature=temperature)
    except Exception:
        return original
    refined = _sanitize(raw, max_chars)
    # Guard against a degenerate rewrite (empty / lost the question entirely).
    if len(refined) < 2:
        return original
    return refined
