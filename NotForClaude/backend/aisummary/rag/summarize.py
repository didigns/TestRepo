"""On-demand document summarization for the chat flow.

Detects a "summarize this" request and gathers what to summarize — a specific
named document (whole-doc coverage) or, failing that, the retrieved topic set.
The engine then produces a grounded, cited, structured summary: the same
anti-hallucination guardrail as Q&A (sources only + [Sn] citations), but shaped
as a summary instead of an answer. Surfaces as a `summarize_document` tool.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .vectorstore import Retrieved

# Action forms only ("요약해/정리해/간추려/summarize"), so a question *about*
# summaries ("요약 기능이 뭐야?") doesn't trigger it.
_SUMMARY_VERB = re.compile(
    r"(요약\s*(해|하|좀|줘|정리|본|하자)|정리\s*(해|하|좀|줘|하자)|간추려|추려\s*줘|"
    r"한\s*(눈|줄)에|핵심만|요점만|summari[sz]e|\btl;?dr\b|\bsummary\b)",
    re.I,
)


def is_summarize_request(q: str) -> bool:
    """True when the message asks to summarize (an action), not a Q&A turn."""
    return bool(_SUMMARY_VERB.search((q or "").strip()))


def pick_file(rows: List[dict], q: str) -> Optional[str]:
    """If the query names an indexed file, return its source_path (for a
    whole-document summary). Conservative — the filename base (ext stripped,
    >= 3 chars) must appear in the query; otherwise None (topic summary)."""
    ql = (q or "").lower()
    best_path, best_len = None, 0
    for r in rows:
        fn = (r.get("filename") or "")
        base = re.sub(r"\.[^.]+$", "", fn).strip().lower()
        if len(base) >= 3 and base in ql and len(base) > best_len:
            best_path, best_len = r.get("source_path", ""), len(base)
    return best_path or None


def gather_file_chunks(rows: List[dict], source_path: str,
                       max_chunks: int = 48) -> List[Retrieved]:
    """All chunks of one file in document order, evenly sampled down to
    max_chunks so a long document still fits the context with broad coverage."""
    fr = [r for r in rows if r.get("source_path") == source_path]
    fr.sort(key=lambda r: (r.get("page", 0), r.get("char_start", 0)))
    if len(fr) > max_chunks:
        step = len(fr) / max_chunks
        fr = [fr[int(i * step)] for i in range(max_chunks)]
    return [
        Retrieved(
            chunk_id=r["chunk_id"], source_path=r["source_path"],
            filename=r.get("filename", ""), page=r["page"],
            char_start=r["char_start"], char_end=r["char_end"],
            text=r["text"], score=1.0,
        )
        for r in fr
    ]


SUMMARIZE_SYSTEM = (
    "당신은 사용자의 로컬 문서를 요약하는 어시스턴트입니다. 아래 제공된 출처 내용만 "
    "사용해 구조화된 요약을 작성하세요.\n"
    "규칙:\n"
    "1) 출처에 있는 내용만 사용하세요. 외부 지식·추측 금지, 없는 내용을 지어내지 마세요.\n"
    "2) 각 사실에는 근거 출처를 [S1], [S2] 형식으로만 인용하세요.\n"
    "3) 읽기 쉬운 **마크다운**으로 작성: 범주는 굵은 소제목(`**제목**`), 항목은 불릿(`- `). "
    "속성 비교·수치·구분 값은 마크다운 **표**(`| 열 | 열 |`)로 정리.\n"
    "4) 서두 인사말·군더더기 없이 핵심만. 같은 내용·같은 인용을 반복하지 마세요.\n"
    "5) 출처에 요약할 내용이 전혀 없으면 정확히 이렇게만 답하세요: "
    "'제공된 문서에서 요약할 내용을 찾을 수 없습니다.'"
)

SUMMARIZE_REFUSAL = "제공된 문서에서 요약할 내용을 찾을 수 없습니다."
