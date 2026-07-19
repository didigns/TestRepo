"""Document summarization: intent detection, target resolution, engine routing.

Runs without a model (fake provider) or native deps (NumPy fallback store).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisummary.config import Settings
from aisummary.rag.engine import RagEngine
from aisummary.rag.summarize import (
    SUMMARIZE_SYSTEM, gather_file_chunks, is_summarize_request, pick_file,
)

from test_core import FakeProvider, _store_with  # reuse existing fakes


# ---- intent detection --------------------------------------------------------


def test_intent_true_on_summarize_commands():
    for q in ["이 문서 요약해줘", "시장조사 한것도 정리해줘", "핵심만 알려줘",
              "요점만 추려줘", "summarize this", "tl;dr 부탁"]:
        assert is_summarize_request(q), q


def test_intent_false_on_questions():
    for q in ["요약 기능이 뭐야?", "계약 만료일은?", "InfoPilot이 뭐야",
              "이 앱은 어떻게 설치해?"]:
        assert not is_summarize_request(q), q


# ---- target resolution -------------------------------------------------------


def test_pick_file_matches_named_file_else_none():
    rows = [{"filename": "market_report.pdf", "source_path": "/x/market_report.pdf"},
            {"filename": "notes.txt", "source_path": "/x/notes.txt"}]
    assert pick_file(rows, "market_report 요약해줘") == "/x/market_report.pdf"
    assert pick_file(rows, "아무 문서나 정리해줘") is None


def test_gather_file_chunks_orders_and_samples():
    rows = [{"chunk_id": f"c{i}", "source_path": "/d.pdf", "filename": "d.pdf",
             "page": 1, "char_start": i, "char_end": i + 1, "text": f"t{i}"}
            for i in range(100)]
    got = gather_file_chunks(rows, "/d.pdf", max_chunks=10)
    assert len(got) == 10                                  # sampled down
    assert got[0].char_start <= got[-1].char_start         # document order
    assert all(g.source_path == "/d.pdf" for g in got)


# ---- engine routing ----------------------------------------------------------


class ScriptedProvider(FakeProvider):
    """Returns the scripted summary when asked with the summarize system prompt;
    records that it was used. Refine/other calls return empty."""

    def __init__(self, settings, summary="문서 요약입니다 [S1]."):
        super().__init__(settings, reply=summary)
        self.used_summarize_system = False

    def generate(self, prompt, system="", model=None, temperature=None):
        if system == SUMMARIZE_SYSTEM:
            self.used_summarize_system = True
            return self.reply
        return ""


def _engine(summary="문서 요약입니다 [S1]."):
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, ["the contract expires on march third",
                            "renewal requires 30 day notice"])
    return RagEngine(s, store, ScriptedProvider(s, summary)), s


def test_query_routes_to_summarize_and_tags_tool():
    eng, _ = _engine()
    ans = eng.query("이 문서 요약해줘")
    assert eng.provider.used_summarize_system            # summarize prompt, not QA
    assert ans.tools and ans.tools[0]["name"] == "summarize_document"
    assert ans.tools[0]["title"] == "문서 요약"
    assert ans.grounded and ans.citations                # cited summary (grounding kept)


def test_non_summarize_query_stays_qa():
    eng, _ = _engine()
    ans = eng.query("계약 만료일은?")
    assert not eng.provider.used_summarize_system
    assert not any((t.get("name") == "summarize_document") for t in (ans.tools or []))


def test_summarize_stream_meta_carries_tool():
    eng, _ = _engine()
    events = list(eng.query_stream("정리해줘"))
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["tools"][0]["name"] == "summarize_document"
    assert any(e["type"] == "token" for e in events)
    assert events[-1]["type"] == "done"


def test_summarize_files_scope_targets_mentioned_file():
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, ["alpha contract content", "beta invoice content"])
    eng = RagEngine(s, store, ScriptedProvider(s, "요약입니다 [S1]."))
    ans = eng.query("요약해줘", files=["/f1.txt"])          # @-mention: only the 2nd file
    assert ans.tools[0]["name"] == "summarize_document"
    assert "f1.txt" in str(ans.tools[0]["input"]["대상"])   # scoped to the mentioned file
    # the summarized context came only from f1 (its chunk is the sole source)
    assert all(c.filename == "f1.txt" for c in ans.citations)


def test_summarize_refuses_on_empty_store():
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, [])            # nothing indexed
    eng = RagEngine(s, store, ScriptedProvider(s))
    ans = eng.query("전체 요약해줘")
    assert ans.refused
    assert ans.tools[0]["name"] == "summarize_document"
