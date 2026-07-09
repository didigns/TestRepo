"""Grounded RAG query engine — the hallucination-suppression core.

Pipeline (see ARCHITECTURE.md §4.2 / §6):
  1. embed query -> vector search
  2. confidence gate: if best similarity < threshold -> refuse ("not found")
  3. pack labeled sources [S1..Sn] into a grounding prompt
  4. generate with low temperature + citation-forcing system prompt
  5. post-verify: every cited [Sn] must exist; flag uncited factual claims
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import Settings
from ..llm import OllamaProvider
from .vectorstore import VectorStore, Retrieved
from .hybrid import BM25, reciprocal_rank_fusion

SYSTEM = (
    "당신은 사용자의 로컬 문서에 대해서만 답하는 어시스턴트입니다.\n"
    "규칙:\n"
    "1) 아래 제공된 출처의 내용만 사용하세요. 외부 지식이나 추측 금지.\n"
    "2) 사실에는 근거 출처를 [S1], [S2] 형식으로만 인용하세요 "
    "(①②③ 같은 기호 금지).\n"
    "3) 출처에 답이 없으면 정확히 이렇게만 답하세요: "
    "'제공된 문서에서 관련 내용을 찾을 수 없습니다.'\n"
    "4) 없는 사실을 지어내지 마세요.\n"
    "형식 규칙 (반드시 지킬 것):\n"
    "- 읽기 쉬운 **마크다운**으로 작성. 서두 인사말·불필요한 도입부 없이 바로 핵심.\n"
    "- 항목이 여러 개면 불릿 목록(`- `)으로, 범주가 나뉘면 굵은 소제목(`**제목**`)으로 묶기.\n"
    "- 같은 문장·같은 인용을 반복하지 말 것. 각 요점은 한 번만.\n"
    "- 간결하게. 한 요점은 1~2문장."
)

REFUSAL = "제공된 문서에서 관련 내용을 찾을 수 없습니다."

# Free / general mode: used when retrieval is weak (no confident document match).
# Keeps the anti-hallucination guardrail for the user's *documents*, but lets the
# assistant answer general / conversational / reasoning questions naturally
# instead of refusing.
GENERAL_SYSTEM = (
    "당신은 AISummary의 친절하고 유능한 AI 비서입니다.\n"
    "이 질문은 사용자의 로컬 문서에서 뚜렷한 근거를 찾지 못했습니다. "
    "일반 지식과 상식으로 대화하듯 자연스럽고 도움이 되게 답하세요.\n"
    "- 짧은 질문엔 짧게, 복잡하면 요점을 잡아 명확하게. 딱딱한 거부 문구는 쓰지 마세요.\n"
    "- 추론과 일반 지식을 자유롭게 활용하되, 사용자의 특정 문서 내용을 아는 척 지어내지 마세요.\n"
    "- 특정 파일·자료의 내용을 묻는데 근거가 없으면, 그 내용은 찾지 못했다고 솔직히 말한 뒤 "
    "일반적인 관점에서 도와주세요.\n"
    "- [S1] 같은 출처 인용 표기는 쓰지 마세요 (참조할 근거 문서가 없습니다)."
)

# Any citation marker a model might emit: [S1] · [1] · 【1】 · ①⑳ ❶❿ ➀➓ ⓪
_CITE_ANY = re.compile(r"\[S?\d+\]|【\d+】|[①-⑳❶-❿➀-➓⓪]")


def _with_persona(base: str, persona: Optional[str]) -> str:
    """Append a plugin's persona/instructions AFTER the core rules, so the
    grounding + citation guardrails always remain in force."""
    persona = (persona or "").strip()
    if not persona:
        return base
    return base + "\n\n[플러그인 역할·지침 — 위 규칙은 그대로 지키면서 아래 성격으로 답하세요]\n" + persona


def _cited_numbers(raw: str) -> set:
    """Extract cited source numbers regardless of marker style."""
    nums = set()
    for m in re.finditer(r"\[S?(\d+)\]|【(\d+)】", raw):
        nums.add(int(m.group(1) or m.group(2)))
    for ch in raw:
        o = ord(ch)
        if 0x2460 <= o <= 0x2473:        # ① .. ⑳
            nums.add(o - 0x2460 + 1)
        elif 0x2776 <= o <= 0x277E:      # ❶ .. ❾
            nums.add(o - 0x2776 + 1)
        elif 0x2780 <= o <= 0x2792:      # ➀ .. ⑲
            nums.add(o - 0x2780 + 1)
        elif o == 0x24EA:                # ⓪
            nums.add(0)
    return nums


@dataclass
class Citation:
    label: str            # "S1"
    filename: str
    page: int
    source_path: str
    snippet: str
    score: float


@dataclass
class Answer:
    text: str
    citations: List[Citation] = field(default_factory=list)
    grounded: bool = True
    confidence: float = 0.0
    warnings: List[str] = field(default_factory=list)
    refused: bool = False


class RagEngine:
    def __init__(self, settings: Settings, store: VectorStore, provider: OllamaProvider):
        self.settings = settings
        self.store = store
        self.provider = provider
        self._bm25 = None            # cached BM25 index
        self._bm25_rows = None       # rows aligned to the BM25 corpus
        self._bm25_count = -1        # store.count() the cache was built at

    def _get_bm25(self):
        """Build (and cache) a BM25 index over all chunks; rebuild on change."""
        from .hybrid import tokenize
        n = self.store.count()
        if self._bm25 is None or self._bm25_count != n:
            rows = self.store.all_rows()
            self._bm25 = BM25([tokenize(r["text"]) for r in rows])
            self._bm25_rows = rows
            self._bm25_count = n
        return self._bm25, self._bm25_rows

    def _retrieve(self, question: str):
        """Hybrid retrieval. Returns (ranked Retrieved list, best_vec_sim)."""
        prof = self.settings.profile()
        topk = self.settings.top_k_override or prof.top_k
        pool = max(topk * 2, 10)
        qvec = self.provider.embed(question)
        vhits = self.store.search(qvec, top_k=pool)
        best = max((h.score for h in vhits), default=0.0)

        if not self.settings.hybrid_search or self.store.count() == 0:
            return vhits[:topk], best

        bm25, rows = self._get_bm25()
        bm_top = bm25.top(question, n=pool)
        by_id = {h.chunk_id: h for h in vhits}
        for i, _ in bm_top:
            r = rows[i]
            by_id.setdefault(r["chunk_id"], Retrieved(
                chunk_id=r["chunk_id"], source_path=r["source_path"],
                filename=r.get("filename", ""), page=r["page"],
                char_start=r["char_start"], char_end=r["char_end"],
                text=r["text"], score=0.0))
        fused = reciprocal_rank_fusion(
            [[h.chunk_id for h in vhits], [rows[i]["chunk_id"] for i, _ in bm_top]])
        ranked_ids = sorted(fused, key=lambda k: fused[k], reverse=True)
        ranked = [by_id[i] for i in ranked_ids if i in by_id][:topk]
        return ranked, best

    def _general_answer(self, question: str, best: float,
                        persona: Optional[str] = None) -> Answer:
        """Free-form answer when retrieval finds no confident document match.
        No citations, no warnings, no refusal banner — just a natural reply."""
        raw = self.provider.generate(
            question, system=_with_persona(GENERAL_SYSTEM, persona),
            temperature=getattr(self.settings, "general_temperature", 0.6))
        return Answer(text=raw, citations=[], grounded=False,
                      confidence=best, warnings=[], refused=False)

    def _scope_hits(self, hits, folders):
        """Restrict retrieved hits to files under the allowed folders. `None`
        or `["*"]` means unrestricted. Used to enforce a plugin's granted
        folder permissions — a plugin only ever sees what it's allowed to."""
        if not folders or "*" in folders:
            return hits
        import os
        allow = [os.path.normpath(f) for f in folders if f]
        if not allow:
            return hits

        def ok(h):
            sp = os.path.normpath(getattr(h, "source_path", "") or "")
            return any(sp == a or sp.startswith(a + os.sep) for a in allow)
        return [h for h in hits if ok(h)]

    def query(self, question: str, persona: Optional[str] = None,
              folders: Optional[list] = None) -> Answer:
        prof = self.settings.profile()
        hits, best = self._retrieve(question)
        hits = self._scope_hits(hits, folders)

        # ---- confidence gate: weak match -> general mode (or refuse) ----
        if not hits or best < self.settings.similarity_threshold:
            if getattr(self.settings, "general_mode", True):
                return self._general_answer(question, best, persona)
            return Answer(text=REFUSAL, grounded=True, confidence=best,
                          refused=True,
                          warnings=["검색 신뢰도가 임계값 미만 → 답변 거부"])

        # ---- build labeled context ----
        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n"
                  f"# 질문\n{question}\n\n"
                  f"# 답변 (각 사실에 [Sn] 인용, 없으면 거부 문구)")
        raw = self.provider.generate(prompt, system=_with_persona(SYSTEM, persona),
                                     temperature=self.settings.temperature)

        return self._verify(raw, labeled, best)

    def query_stream(self, question: str, persona: Optional[str] = None,
                     folders: Optional[list] = None):
        """Streaming variant. Yields dict events:
        {type:'meta', refused, confidence} → {type:'token', text} … →
        {type:'done', grounded, refused, citations, warnings}."""
        prof = self.settings.profile()
        hits, best = self._retrieve(question)
        hits = self._scope_hits(hits, folders)

        if not hits or best < self.settings.similarity_threshold:
            # Weak match -> general mode (natural free-form answer), unless the
            # user disabled it, in which case keep the original hard refusal.
            if getattr(self.settings, "general_mode", True):
                gtemp = getattr(self.settings, "general_temperature", 0.6)
                yield {"type": "meta", "refused": False, "confidence": round(best, 4)}
                gsys = _with_persona(GENERAL_SYSTEM, persona)
                buf = []
                try:
                    for tok in self.provider.generate_stream(
                            question, system=gsys, temperature=gtemp):
                        buf.append(tok)
                        yield {"type": "token", "text": tok}
                except Exception:  # provider without streaming -> one-shot
                    raw = self.provider.generate(
                        question, system=gsys, temperature=gtemp)
                    yield {"type": "token", "text": raw}
                yield {"type": "done", "grounded": False, "refused": False,
                       "citations": [], "warnings": []}
                return

            yield {"type": "meta", "refused": True, "confidence": round(best, 4)}
            yield {"type": "token", "text": REFUSAL}
            yield {"type": "done", "grounded": True, "refused": True,
                   "citations": [], "warnings": ["검색 신뢰도가 임계값 미만"]}
            return

        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n# 질문\n{question}\n\n"
                  f"# 답변 (각 사실에 [Sn] 인용, 없으면 거부 문구)")
        yield {"type": "meta", "refused": False, "confidence": round(best, 4)}

        gsys = _with_persona(SYSTEM, persona)
        buf = []
        try:
            for tok in self.provider.generate_stream(prompt, system=gsys):
                buf.append(tok)
                yield {"type": "token", "text": tok}
        except Exception as e:  # provider without streaming → one-shot
            raw = self.provider.generate(prompt, system=gsys)
            buf = [raw]
            yield {"type": "token", "text": raw}

        ans = self._verify("".join(buf), labeled, best)
        yield {"type": "done", "grounded": ans.grounded, "refused": ans.refused,
               "citations": [c.__dict__ for c in ans.citations],
               "warnings": ans.warnings}

    def free_stream(self, prompt: str, persona: Optional[str] = None,
                    temperature: Optional[float] = None):
        """Free (ungrounded) generation for flow `generate` steps with
        grounded=false — creative/interactive use. No retrieval, no citations."""
        sys = _with_persona(GENERAL_SYSTEM, persona)
        temp = temperature if temperature is not None else getattr(
            self.settings, "general_temperature", 0.7)
        yield {"type": "meta", "refused": False, "confidence": 0.0}
        try:
            for tok in self.provider.generate_stream(prompt, system=sys, temperature=temp):
                yield {"type": "token", "text": tok}
        except Exception:
            raw = self.provider.generate(prompt, system=sys, temperature=temp)
            yield {"type": "token", "text": raw}
        yield {"type": "done", "grounded": False, "refused": False,
               "citations": [], "warnings": []}

    # ---- helpers -----------------------------------------------------
    def _pack(self, hits: List[Retrieved], budget_tokens: int):
        """Pack chunks into a token budget, labeling each [S1..Sn]."""
        approx_chars = budget_tokens * 3  # ~3 chars/token, conservative
        lines, labeled, used = [], {}, 0
        for i, h in enumerate(hits, start=1):
            label = f"S{i}"
            block = f"[{label}] ({h.filename} p.{h.page}) {h.text}"
            if used + len(block) > approx_chars and labeled:
                break
            lines.append(block)
            labeled[label] = h
            used += len(block)
        return "\n\n".join(lines), labeled

    def _verify(self, raw: str, labeled: dict, best: float) -> Answer:
        warnings: List[str] = []
        if REFUSAL in raw or raw.strip() == REFUSAL:
            return Answer(text=REFUSAL, grounded=True, confidence=best,
                          refused=True)

        nums = _cited_numbers(raw)
        valid = {f"S{n}" for n in nums if f"S{n}" in labeled}
        invalid = {n for n in nums if f"S{n}" not in labeled}
        if invalid:
            warnings.append(f"존재하지 않는 인용: {sorted(invalid)}")

        # Flag factual sentences with no citation (post-check).
        if self.settings.require_citations:
            uncited = self._uncited_sentences(raw)
            if uncited:
                warnings.append(f"인용 없는 문장 {len(uncited)}개 감지")

        citations = [Citation(
            label=lab, filename=labeled[lab].filename, page=labeled[lab].page,
            source_path=labeled[lab].source_path,
            snippet=labeled[lab].text[:240], score=labeled[lab].score,
        ) for lab in sorted(valid, key=lambda x: int(x[1:]))]

        grounded = bool(valid) and not invalid
        return Answer(text=raw, citations=citations, grounded=grounded,
                      confidence=best, warnings=warnings,
                      refused=False)

    @staticmethod
    def _uncited_sentences(text: str) -> List[str]:
        out = []
        for sent in re.split(r"(?<=[.!?。])\s+", text.strip()):
            s = sent.strip()
            if len(s) < 15:
                continue
            if not _CITE_ANY.search(s):
                out.append(s)
        return out
