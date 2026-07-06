"""Eval harness + meeting minutes tests (no Ollama needed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ownyourpc.config import Settings
from ownyourpc.rag.engine import RagEngine
from ownyourpc.rag.evaluate import GoldenCase, evaluate
from ownyourpc.meetings.transcribe import Transcript, Segment
from ownyourpc.meetings.minutes import _extract_json, Minutes, render_pdf

# reuse fake infra from test_core
from test_core import _store_with, FakeProvider


def test_eval_harness_scores_perfect_on_scripted():
    s = Settings(tier="low", similarity_threshold=0.0)
    store = _store_with(s, ["the contract expires on march third 2026"])

    class ScriptedEngine(RagEngine):
        def query(self, question):
            prov = FakeProvider(self.settings,
                                reply="계약 만료일은 3월 3일 [S1].")
            self.provider = prov
            return super().query(question)

    eng = ScriptedEngine(s, store, FakeProvider(s))
    cases = [GoldenCase(question="contract expire", expected_terms=["3월"])]
    res = evaluate(eng, cases)
    assert res.groundedness == 1.0
    assert res.citation_accuracy == 1.0
    assert res.rag_score >= 85


def test_eval_harness_rewards_correct_refusal():
    s = Settings(tier="low", similarity_threshold=0.99)  # force refuse
    store = _store_with(s, ["irrelevant banana text"])
    eng = RagEngine(s, store, FakeProvider(s, reply="ignored"))
    cases = [GoldenCase(question="unrelated physics", should_refuse=True)]
    res = evaluate(eng, cases)
    assert res.refusal_correctness == 1.0
    assert res.rag_score >= 85


def test_extract_json_defensive():
    assert _extract_json('noise {"summary": "s"} tail') == {"summary": "s"}
    assert _extract_json("no json here") == {}
    assert _extract_json('{"bad": ') == {}


def test_render_pdf_produces_file(tmp_path):
    tr = Transcript(meeting_id="mtest", title="주간 회의", started_at=0.0,
                    segments=[Segment(0, 2, "안건 논의", speaker="화자 1"),
                              Segment(2, 4, "다음 주 배포 결정")])
    m = Minutes(title="주간 회의", summary="배포 일정 논의",
                key_topics=["배포"], decisions=["다음 주 배포"],
                action_items=[{"task": "릴리스 노트", "owner": "홍길동", "due": "금요일"}])
    out = tmp_path / "minutes.pdf"
    p = render_pdf(m, tr, out)
    assert p.exists() and p.stat().st_size > 500
    assert p.read_bytes()[:4] == b"%PDF"
