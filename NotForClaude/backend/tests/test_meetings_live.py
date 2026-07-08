"""Streaming meeting transcription tests — no mic, no whisper model.

Injects a fake transcribe_fn and synthetic audio so the buffering, window
flushing, and absolute-time adjustment logic are verified deterministically.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from aisummary.config import Settings
from aisummary.meetings.transcribe import Segment
from aisummary.meetings.streaming import StreamingTranscriber
from aisummary.meetings.capture import save_wav, SAMPLE_RATE


def _fake_fn_factory():
    """Returns a transcribe_fn that emits one segment per call, tagged with
    the call index so we can check time-shifting."""
    calls = {"n": 0}

    def fn(audio):
        i = calls["n"]
        calls["n"] += 1
        # window-relative segment 0.0-1.0s
        return [Segment(start=0.0, end=1.0, text=f"utterance {i}")]
    return fn


def test_streaming_flushes_on_full_window():
    s = Settings()
    st = StreamingTranscriber(s, window_sec=5.0, transcribe_fn=_fake_fn_factory())
    # feed 4s -> no flush yet
    out = st.feed(np.zeros(SAMPLE_RATE * 4, dtype="float32"))
    assert out == []
    # feed 2 more seconds -> crosses 5s window -> flush
    out = st.feed(np.zeros(SAMPLE_RATE * 2, dtype="float32"))
    assert len(out) == 1
    assert out[0].text == "utterance 0"


def test_streaming_time_is_absolute_across_windows():
    s = Settings()
    st = StreamingTranscriber(s, window_sec=2.0, transcribe_fn=_fake_fn_factory())
    first = st.feed(np.zeros(SAMPLE_RATE * 2, dtype="float32"))
    second = st.feed(np.zeros(SAMPLE_RATE * 2, dtype="float32"))
    assert first[0].start == 0.0            # window 1 starts at 0
    assert second[0].start == 2.0           # window 2 shifted by 2s


def test_finalize_drains_remaining():
    s = Settings()
    st = StreamingTranscriber(s, window_sec=10.0, transcribe_fn=_fake_fn_factory())
    assert st.feed(np.zeros(SAMPLE_RATE * 3, dtype="float32")) == []
    tail = st.finalize()                    # 3s < window, flushed on finalize
    assert len(tail) == 1


def test_save_wav_writes_valid_pcm(tmp_path):
    audio = (np.sin(np.linspace(0, 100, SAMPLE_RATE)) * 0.5).astype("float32")
    p = save_wav(tmp_path / "a.wav", audio)
    assert p.exists()
    data = p.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def test_run_live_meeting_with_fakes():
    """End-to-end orchestration with a fake capture + fake transcriber."""
    import threading
    from aisummary.meetings.streaming import run_live_meeting

    class FakeCapture:
        def __init__(self):
            self.started = self.stopped = False

        def start(self):
            self.started = True

        def stream(self):
            # 3 x 2s chunks then end
            for _ in range(3):
                yield np.zeros(SAMPLE_RATE * 2, dtype="float32")

        def stop(self):
            self.stopped = True

    s = Settings()
    stop = threading.Event()
    got = []
    tr = run_live_meeting(s, "테스트 회의", got.append, stop,
                          capture=FakeCapture(),
                          transcribe_fn=_fake_fn_factory())
    assert tr.title == "테스트 회의"
    assert len(tr.segments) >= 1
    assert tr.segments == got
