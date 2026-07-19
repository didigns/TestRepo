"""Query refinement: unit behavior + engine wiring.

Refinement is retrieval-only — the refined text drives search, the answer is
generated against the user's original question. Runs without a model (fake
provider) or native deps (NumPy fallback store).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisummary.config import Settings
from aisummary.rag.engine import RagEngine, REFUSAL
from aisummary.rag.refine import REFINE_SYSTEM, _sanitize, refine_query

from test_core import FakeProvider, _store_with  # reuse existing fakes


# ---- refine_query unit -------------------------------------------------------


class _Prov:
    def __init__(self, out):
        self.out = out
        self.calls = []

    def generate(self, prompt, system="", model=None, temperature=None):
        self.calls.append((prompt, system, temperature))
        return self.out


def test_rewrites_and_passes_low_temperature():
    p = _Prov("계약 만료일·종료일·갱신 조건 조항")
    assert refine_query(p, "계약 관련 알려줘") == "계약 만료일·종료일·갱신 조건 조항"
    assert p.calls[0][1] == REFINE_SYSTEM
    assert p.calls[0][2] == 0.0


def test_sanitize_strips_label_quotes_and_extra_lines():
    assert _sanitize('검색 질의: "프로젝트 일정과 마일스톤"\n(부연설명)', 300) == "프로젝트 일정과 마일스톤"


def test_empty_question_unchanged():
    assert refine_query(_Prov("x"), "   ") == "   "


def test_provider_error_falls_back_to_original():
    class Boom:
        def generate(self, *a, **k):
            raise RuntimeError("model down")

    assert refine_query(Boom(), "원래 질문") == "원래 질문"


def test_degenerate_rewrite_falls_back():
    assert refine_query(_Prov(""), "원래 질문") == "원래 질문"


# ---- engine wiring -----------------------------------------------------------


class ScriptedProvider(FakeProvider):
    """Returns the refined query on refine calls, the answer otherwise. Records
    which text was embedded (retrieval) and the answer prompts."""

    def __init__(self, settings, refined, answer):
        super().__init__(settings, reply=answer)
        self.refined = refined
        self.embed_calls = []
        self.answer_prompts = []

    def embed(self, text, model=None):
        self.embed_calls.append(text)
        return super().embed(text, model)

    def generate(self, prompt, system="", model=None, temperature=None):
        if system == REFINE_SYSTEM:
            return self.refined
        self.answer_prompts.append(prompt)
        return self.reply


def _engine(refined, answer="답변 [S1].", **skw):
    s = Settings(tier="low", similarity_threshold=0.0, **skw)
    store = _store_with(s, ["the contract expires on march third"])
    prov = ScriptedProvider(s, refined=refined, answer=answer)
    return RagEngine(s, store, prov), prov


def test_retrieval_uses_refined_answer_uses_original():
    eng, prov = _engine(refined="계약 만료일 종료일 갱신")
    ans = eng.query("계약")
    # refined query surfaced, and it's what got embedded for retrieval
    assert ans.refined_query == "계약 만료일 종료일 갱신"
    assert prov.embed_calls == ["계약 만료일 종료일 갱신"]
    # the answer prompt carries the ORIGINAL question, not the refined one
    prompt = prov.answer_prompts[0]
    assert "# 질문\n계약" in prompt
    assert "만료일" not in prompt  # refined-only token must not reach the answer
    # generic tool record for the "Tool called" UI
    assert ans.tools == [{
        "name": "refine_query", "title": "질의 다듬기",
        "input": {"질문": "계약"}, "output": {"검색 질의": "계약 만료일 종료일 갱신"},
    }]


def test_refine_false_override_uses_original_for_retrieval():
    eng, prov = _engine(refined="계약 만료일 종료일")
    ans = eng.query("계약", refine=False)
    assert ans.refined_query == ""
    assert ans.tools == []
    assert prov.embed_calls == ["계약"]  # original embedded, no refine call


def test_setting_disables_refinement():
    eng, prov = _engine(refined="계약 만료일 종료일", refine_query=False)
    ans = eng.query("계약")
    assert ans.refined_query == ""
    assert prov.embed_calls == ["계약"]


def test_refined_query_empty_when_unchanged():
    # refine returns the same text -> nothing to surface
    eng, prov = _engine(refined="계약")
    ans = eng.query("계약")
    assert ans.refined_query == ""
    assert ans.tools == []


def test_stream_meta_carries_refined_query():
    eng, prov = _engine(refined="계약 만료일 종료일")
    events = list(eng.query_stream("계약"))
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["refined_query"] == "계약 만료일 종료일"
    assert meta["tools"][0]["name"] == "refine_query"
    assert meta["tools"][0]["output"] == {"검색 질의": "계약 만료일 종료일"}
