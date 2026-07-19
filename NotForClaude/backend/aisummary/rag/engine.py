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
from .refine import refine_query
from .summarize import (
    is_summarize_request, pick_file, gather_file_chunks,
    SUMMARIZE_SYSTEM, SUMMARIZE_REFUSAL,
)

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
    "- 여러 항목의 속성 비교·수치·구분 값 등 표로 정리하면 더 명확한 내용은 마크다운 **표**로 작성하기 "
    "(`| 열1 | 열2 |` 헤더 + `| --- | --- |` 구분선 + 데이터 행). 표 셀 안에도 필요하면 [Sn] 인용.\n"
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
    # The search-optimized rewrite used for retrieval (empty if refinement was
    # off or left the question unchanged). The answer itself is always generated
    # against the user's original question.
    refined_query: str = ""
    # Generic record of tools that ran this turn, for the UI's "Tool called"
    # display: [{name, title, input:{...}, output:{...}}]. Query refinement is
    # the first; future function-tools reuse the same shape.
    tools: List[dict] = field(default_factory=list)


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

    def _tool_calls(self, question: str, search_q: str) -> List[dict]:
        """Structured record of tools that ran this turn, for the UI's generic
        'Tool called' display. Currently just query refinement (only when it
        actually changed the query). Future function-tools append here too."""
        tools: List[dict] = []
        if search_q and search_q != question:
            tools.append({
                "name": "refine_query",
                "title": "질의 다듬기",
                "input": {"질문": question},
                "output": {"검색 질의": search_q},
            })
        return tools

    def _refine(self, question: str, override: Optional[bool] = None) -> str:
        """Rewrite the question for retrieval. Returns the search query to use;
        the original question is what the answer is generated against.

        Enabled by settings.refine_query (default on) unless `override` is given.
        Always safe — refine_query falls back to the original on any failure."""
        on = getattr(self.settings, "refine_query", True) if override is None else override
        if not on:
            return question
        return refine_query(
            self.provider, question,
            temperature=getattr(self.settings, "refine_temperature", 0.0),
        )

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

    # ---- document summarization ------------------------------------------

    def _summary_target(self, question, folders, refine, files=None):
        """Resolve what to summarize. Priority: explicit @-mention files ->
        named file in the text -> retrieved topic set. Returns (hits, best_sim,
        scope_label)."""
        rows = self.store.all_rows()
        if files:                                   # explicit @-mention targets
            hits = []
            for fp in files:
                hits += gather_file_chunks(rows, fp)
            names = sorted({h.filename for h in hits})
            label = names[0] if len(names) == 1 else f"파일 {len(names)}개"
            return hits, 1.0, label
        src = pick_file(rows, question)
        if src:
            hits = self._scope_hits(gather_file_chunks(rows, src), folders)
            scope = hits[0].filename if hits else src
            return hits, 1.0, scope
        search_q = self._refine(question, refine)
        hits, best = self._retrieve(search_q)
        hits = self._scope_hits(hits, folders)
        names = sorted({h.filename for h in hits})
        scope = names[0] if len(names) == 1 else f"관련 문서 {len(names)}개"
        return hits, best, scope

    @staticmethod
    def _summary_tools(scope, n):
        return [{
            "name": "summarize_document", "title": "문서 요약",
            "input": {"대상": scope}, "output": {"요약 범위": f"청크 {n}개"},
        }]

    def _summarize(self, question, persona=None, folders=None, refine=None, files=None) -> Answer:
        prof = self.settings.profile()
        hits, best, scope = self._summary_target(question, folders, refine, files)
        tools = self._summary_tools(scope, len(hits))
        if not hits:
            return Answer(text=SUMMARIZE_REFUSAL, grounded=True, confidence=best,
                          refused=True, tools=tools)
        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n"
                  f"# 작업\n위 문서 내용을 구조화해 요약하세요 (각 요점에 [Sn] 인용).")
        raw = self.provider.generate(
            prompt, system=_with_persona(SUMMARIZE_SYSTEM, persona),
            temperature=self.settings.temperature)
        ans = self._verify(raw, labeled, best)
        ans.tools = tools
        return ans

    def _summarize_stream(self, question, persona=None, folders=None, refine=None, files=None):
        prof = self.settings.profile()
        hits, best, scope = self._summary_target(question, folders, refine, files)
        tools = self._summary_tools(scope, len(hits))
        if not hits:
            yield {"type": "meta", "refused": True, "confidence": round(best, 4), "tools": tools}
            yield {"type": "token", "text": SUMMARIZE_REFUSAL}
            yield {"type": "done", "grounded": True, "refused": True,
                   "citations": [], "warnings": []}
            return
        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n"
                  f"# 작업\n위 문서 내용을 구조화해 요약하세요 (각 요점에 [Sn] 인용).")
        yield {"type": "meta", "refused": False, "confidence": round(best, 4), "tools": tools}
        gsys = _with_persona(SUMMARIZE_SYSTEM, persona)
        buf = []
        try:
            for tok in self.provider.generate_stream(prompt, system=gsys):
                buf.append(tok)
                yield {"type": "token", "text": tok}
        except Exception:  # provider without streaming -> one-shot
            raw = self.provider.generate(prompt, system=gsys)
            buf = [raw]
            yield {"type": "token", "text": raw}
        ans = self._verify("".join(buf), labeled, best)
        yield {"type": "done", "grounded": ans.grounded, "refused": ans.refused,
               "citations": [c.__dict__ for c in ans.citations], "warnings": ans.warnings}

    # ---- QA retrieval (with @-mention file scoping) ----------------------

    def _strip_mentions(self, question, files):
        """Drop "@<filename>" tokens (the scope rides in `files`), so the real
        ask drives ranking + the answer prompt instead of the file name."""
        import os
        q = question or ""
        for fp in files or []:
            q = q.replace("@" + os.path.basename(fp), " ")
        q = " ".join(q.split()).strip()
        return q or question

    def _files_hits(self, files, query):
        """Chunks to ground on when specific files are @-mentioned: use those
        files directly (NOT global top-k + post-filter, which can drop an
        explicitly-named file whose chunks didn't rank). Small docs use every
        chunk; large docs are ranked by BM25 relevance so the pertinent parts
        survive the context budget."""
        rows = self.store.all_rows()
        chunks = []
        for fp in files or []:
            chunks += gather_file_chunks(rows, fp, max_chunks=10 ** 9)
        if not chunks or len(chunks) <= 12 or not (query or "").strip():
            return chunks
        from .hybrid import BM25, tokenize
        bm = BM25([tokenize(c.text) for c in chunks])
        top = bm.top(query, n=min(len(chunks), 60))
        return [chunks[i] for i, _ in top] if top else chunks

    def _qa_context(self, question, folders, refine, files):
        """Resolve (hits, best, shown_refined, tools, prompt_question). With
        @-mention files, scope to them and skip the (now-misleading) refine."""
        if files:
            import os
            pq = self._strip_mentions(question, files)
            hits = self._files_hits(files, pq)
            names = [os.path.basename(f) for f in files]
            tools = [{"name": "file_scope", "title": "파일 지정",
                      "input": {"파일": ", ".join(names)},
                      "output": {"근거 청크": f"{len(hits)}개"}}]
            return hits, (1.0 if hits else 0.0), "", tools, pq
        search_q = self._refine(question, refine)
        shown = search_q if search_q != question else ""
        tools = self._tool_calls(question, search_q)
        hits, best = self._retrieve(search_q)
        hits = self._scope_hits(hits, folders)
        return hits, best, shown, tools, question

    def query(self, question: str, persona: Optional[str] = None,
              folders: Optional[list] = None, refine: Optional[bool] = None,
              files: Optional[list] = None) -> Answer:
        if is_summarize_request(question):
            return self._summarize(question, persona, folders, refine, files)
        prof = self.settings.profile()
        hits, best, shown, tools, pq = self._qa_context(question, folders, refine, files)

        # An @-mentioned file with no indexed content -> a clear message
        # (not the model's "I can't see the file" hallucination).
        if files and not hits:
            return Answer(text="지정하신 파일에서 인덱싱된 내용을 찾을 수 없습니다.",
                          grounded=True, confidence=0.0, refused=True, tools=tools)
        # ---- confidence gate: open (non-file) search only ----
        if not files and (not hits or best < self.settings.similarity_threshold):
            if getattr(self.settings, "general_mode", True):
                ans = self._general_answer(question, best, persona)
                ans.refined_query = shown; ans.tools = tools
                return ans
            return Answer(text=REFUSAL, grounded=True, confidence=best,
                          refused=True, refined_query=shown, tools=tools,
                          warnings=["검색 신뢰도가 임계값 미만 → 답변 거부"])

        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n"
                  f"# 질문\n{pq}\n\n"
                  f"# 답변 (각 사실에 [Sn] 인용, 없으면 거부 문구)")
        raw = self.provider.generate(prompt, system=_with_persona(SYSTEM, persona),
                                     temperature=self.settings.temperature)

        ans = self._verify(raw, labeled, best)
        ans.refined_query = shown; ans.tools = tools
        return ans

    def query_stream(self, question: str, persona: Optional[str] = None,
                     folders: Optional[list] = None, refine: Optional[bool] = None,
                     files: Optional[list] = None):
        """Streaming variant. Yields dict events:
        {type:'meta', refused, confidence, refined_query} → {type:'token', text}
        … → {type:'done', grounded, refused, citations, warnings}."""
        if is_summarize_request(question):
            yield from self._summarize_stream(question, persona, folders, refine, files)
            return
        prof = self.settings.profile()
        hits, best, shown, tools, pq = self._qa_context(question, folders, refine, files)

        if files and not hits:              # named file with no indexed content
            yield {"type": "meta", "refused": True, "confidence": 0.0,
                   "refined_query": "", "tools": tools}
            yield {"type": "token", "text": "지정하신 파일에서 인덱싱된 내용을 찾을 수 없습니다."}
            yield {"type": "done", "grounded": True, "refused": True,
                   "citations": [], "warnings": []}
            return

        if not files and (not hits or best < self.settings.similarity_threshold):
            # Weak match -> general mode (natural free-form answer), unless the
            # user disabled it, in which case keep the original hard refusal.
            if getattr(self.settings, "general_mode", True):
                gtemp = getattr(self.settings, "general_temperature", 0.6)
                yield {"type": "meta", "refused": False, "confidence": round(best, 4),
                       "refined_query": shown, "tools": tools}
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

            yield {"type": "meta", "refused": True, "confidence": round(best, 4),
                   "refined_query": shown, "tools": tools}
            yield {"type": "token", "text": REFUSAL}
            yield {"type": "done", "grounded": True, "refused": True,
                   "citations": [], "warnings": ["검색 신뢰도가 임계값 미만"]}
            return

        context, labeled = self._pack(hits, prof.context_tokens)
        prompt = (f"# 출처\n{context}\n\n# 질문\n{pq}\n\n"
                  f"# 답변 (각 사실에 [Sn] 인용, 없으면 거부 문구)")
        yield {"type": "meta", "refused": False, "confidence": round(best, 4),
               "refined_query": shown, "tools": tools}

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
