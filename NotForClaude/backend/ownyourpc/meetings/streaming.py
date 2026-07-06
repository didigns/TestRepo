"""Streaming transcription runner.

Buffers incoming audio into fixed windows, transcribes each window with
faster-whisper, and emits time-adjusted Segments as they are produced.
The transcription function is injectable so the orchestration is unit
-testable without a real model or microphone.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from ..config import Settings
from .transcribe import Segment, Transcript, Transcriber
from .capture import MicCapture, SAMPLE_RATE

# transcribe_fn: (audio_float32_1d) -> list[Segment] with window-relative times
TranscribeFn = Callable[[object], List[Segment]]


class StreamingTranscriber:
    def __init__(self, settings: Settings, window_sec: float = 5.0,
                 sample_rate: int = SAMPLE_RATE,
                 transcribe_fn: Optional[TranscribeFn] = None):
        self.settings = settings
        self.sample_rate = sample_rate
        self.window_frames = int(window_sec * sample_rate)
        self._buf: List[object] = []
        self._buffered = 0
        self._elapsed = 0.0            # seconds already emitted
        self._fn = transcribe_fn
        self._transcriber: Optional[Transcriber] = None
        # None => auto-detect on the first window, then LOCK for the session
        # (per-window auto-detection is unreliable on short/quiet audio).
        self._locked_lang: Optional[str] = settings.stt_language

    @property
    def detected_language(self) -> Optional[str]:
        return self._locked_lang

    def _default_fn(self):
        if self._transcriber is None:
            self._transcriber = Transcriber(self.settings)
        model = self._transcriber._load()

        def fn(audio):
            # task="transcribe" 고정 → 절대 영어로 번역하지 않고 원어 그대로 전사
            segs, info = model.transcribe(audio, vad_filter=True,
                                          task="transcribe",
                                          language=self._locked_lang)
            if self._locked_lang is None:            # first-window detection
                det = getattr(info, "language", None)
                if det:
                    self._locked_lang = det          # lock for the session
            return [Segment(start=s.start, end=s.end, text=s.text.strip())
                    for s in segs]
        return fn

    def feed(self, chunk) -> List[Segment]:
        """Add an audio chunk; transcribe & return segments once a full
        window has accumulated (else returns [])."""
        self._buf.append(chunk)
        self._buffered += len(chunk)
        if self._buffered < self.window_frames:
            return []
        return self._flush_window()

    def _flush_window(self) -> List[Segment]:
        import numpy as np
        if not self._buf:
            return []
        audio = np.concatenate(self._buf)
        window_sec = len(audio) / self.sample_rate
        fn = self._fn or self._default_fn()
        segs = fn(audio)
        for s in segs:                 # shift to absolute meeting time
            s.start += self._elapsed
            s.end += self._elapsed
        self._elapsed += window_sec
        self._buf = []
        self._buffered = 0
        return segs

    def finalize(self) -> List[Segment]:
        """Transcribe whatever remains in the buffer."""
        return self._flush_window()


def run_live_meeting(settings: Settings, title: str,
                     on_segment: Callable[[Segment], None],
                     stop_event: threading.Event,
                     capture: Optional[MicCapture] = None,
                     transcribe_fn: Optional[TranscribeFn] = None) -> Transcript:
    """Capture mic audio and transcribe until stop_event is set.

    Returns the accumulated Transcript. `capture`/`transcribe_fn` are
    injectable for testing.
    """
    cap = capture or MicCapture(block_sec=1.0, sample_rate=SAMPLE_RATE)
    st = StreamingTranscriber(settings, transcribe_fn=transcribe_fn)
    tr = Transcript(meeting_id=f"m{int(time.time())}", title=title,
                    started_at=time.time())

    audio_chunks = []                  # keep raw audio for post-meeting diarization
    keep_audio = getattr(settings, "diarize_enabled", False)
    cap.start()
    try:
        for chunk in cap.stream():
            if keep_audio:
                audio_chunks.append(chunk)
            for seg in st.feed(chunk):
                tr.segments.append(seg)
                on_segment(seg)
            if stop_event.is_set():
                break
    finally:
        cap.stop()
    for seg in st.finalize():          # drain remaining audio
        tr.segments.append(seg)
        on_segment(seg)

    if keep_audio and audio_chunks:    # save WAV for diarization on stop
        try:
            import numpy as np
            from .capture import save_wav
            from ..config import MEETINGS_DIR
            MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
            save_wav(MEETINGS_DIR / f"{tr.meeting_id}.wav",
                     np.concatenate(audio_chunks), SAMPLE_RATE)
        except Exception:
            import traceback
            traceback.print_exc()
    return tr
