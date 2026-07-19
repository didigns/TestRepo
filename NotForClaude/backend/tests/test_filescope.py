"""@-mention file scoping in the QA path.

Regression for: an explicitly named file (@file) was dropped when the (refined)
global search didn't rank its chunks into the top-k, so the model refused with
"I can't see the file". Now @-files are grounded directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisummary.config import Settings
from aisummary.rag.engine import RagEngine

from test_core import FakeProvider, _store_with


class QAProvider(FakeProvider):
    """Records the QA prompts it's asked; returns a cited answer."""

    def __init__(self, settings, reply="분석 결과입니다 [S1]."):
        super().__init__(settings, reply=reply)
        self.prompts = []

    def generate(self, prompt, system="", model=None, temperature=None):
        self.prompts.append(prompt)
        return self.reply


def test_at_file_grounds_in_file_and_skips_refine():
    # High threshold: a generic global search would fall through to general mode.
    s = Settings(tier="low", similarity_threshold=0.9)
    store = _store_with(s, ["유저 플로우: 로그인 후 대시보드 이동", "무관한 바나나 내용"])
    prov = QAProvider(s)
    eng = RagEngine(s, store, prov)

    ans = eng.query("@f0.txt 이것도 분석좀", files=["/f0.txt"])

    assert ans.grounded and not ans.refused          # grounded in the file, not refused
    assert ans.refined_query == ""                   # refine skipped for @-file
    assert ans.tools[0]["name"] == "file_scope"      # accurate scope tool (not refine)
    assert "f0.txt" in ans.tools[0]["input"]["파일"]
    assert all(c.filename == "f0.txt" for c in ans.citations)  # scoped to f0
    prompt = prov.prompts[-1]
    assert "이것도 분석좀" in prompt                  # question used...
    assert "@f0.txt" not in prompt                    # ...with the @mention stripped


def test_at_file_with_no_indexed_content_gives_clear_message():
    s = Settings(tier="low")
    store = _store_with(s, ["some indexed content"])   # only /f0.txt exists
    eng = RagEngine(s, store, QAProvider(s))
    ans = eng.query("@ghost.pdf 분석해줘", files=["/ghost.pdf"])
    assert ans.refused
    assert "인덱싱된 내용을 찾을 수 없" in ans.text     # not a hallucinated "can't see it"


def test_at_file_stream_grounds_and_no_refine_tool():
    s = Settings(tier="low", similarity_threshold=0.9)
    store = _store_with(s, ["유저 플로우 상세", "무관 내용"])
    eng = RagEngine(s, store, QAProvider(s))
    events = list(eng.query_stream("@f0.txt 분석", files=["/f0.txt"]))
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["tools"][0]["name"] == "file_scope" and meta["refined_query"] == ""
    assert not meta["refused"]
    assert any(e["type"] == "token" for e in events)
