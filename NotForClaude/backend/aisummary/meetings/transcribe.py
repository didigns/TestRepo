"""Real-time meeting transcription via faster-whisper.

Design: audio is captured into a ring buffer, split on voice activity, and
each utterance is transcribed and pushed to a live subscriber callback
(the UI consumes this over WebSocket). Optional pyannote diarization tags
speakers. faster-whisper/pyannote are imported lazily so this module
imports even before the heavy deps are installed.
"""
from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, List, Optional

from ..config import Settings, MEETINGS_DIR


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None


@dataclass
class Transcript:
    meeting_id: str
    title: str
    started_at: float
    segments: List[Segment] = field(default_factory=list)

    def full_text(self) -> str:
        out = []
        for s in self.segments:
            tag = f"[{s.speaker}] " if s.speaker else ""
            out.append(f"{tag}{s.text}")
        return "\n".join(out)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class Transcriber:
    """Wraps faster-whisper. Streaming and file modes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            size = (getattr(self.settings, "stt_model_override", None)
                    or self.settings.profile().stt_model)
            # CPU int8 by default: base/tiny run faster than real-time on CPU
            # and this avoids the CUDA DLL trap (device="auto" picks CUDA and
            # then crashes with a missing cublas64_*.dll if no CUDA runtime).
            # GPU users can opt in via settings.stt_device = "cuda".
            device = getattr(self.settings, "stt_device", "cpu")
            if device == "cuda":
                # CUDA can construct fine but crash at inference if the CUDA
                # runtime DLLs (cublas/cudnn) are missing — so probe with a
                # tiny transcribe and fall back to CPU on any failure.
                try:
                    import numpy as _np
                    m = WhisperModel(size, device="cuda", compute_type="float16")
                    list(m.transcribe(_np.zeros(16000, dtype="float32"))[0])
                    self._model = m
                except Exception:
                    self._model = None
            if self._model is None:
                self._model = WhisperModel(size, device="cpu", compute_type="int8")
        return self._model

    def transcribe_file(self, audio_path: Path,
                        on_segment: Optional[Callable[[Segment], None]] = None
                        ) -> List[Segment]:
        model = self._load()
        segments, _ = model.transcribe(str(audio_path), vad_filter=True,
                                       task="transcribe",
                                       language=self.settings.stt_language)
        out: List[Segment] = []
        for seg in segments:
            s = Segment(start=seg.start, end=seg.end, text=seg.text.strip())
            out.append(s)
            if on_segment:
                on_segment(s)
        return out


class LiveSession:
    """Manages a live meeting: streams audio chunks -> segments -> callback.

    audio_source is a generator yielding PCM float chunks (from the UI/mic
    layer). This keeps capture (platform-specific) out of the core.
    """

    def __init__(self, settings: Settings, title: str,
                 on_segment: Optional[Callable[[Segment], None]] = None):
        self.settings = settings
        self.transcriber = Transcriber(settings)
        self.transcript = Transcript(
            meeting_id=f"m{int(time.time())}", title=title,
            started_at=time.time())
        self.on_segment = on_segment or (lambda s: None)
        self._running = False

    def add_segment(self, seg: Segment) -> None:
        self.transcript.segments.append(seg)
        self.on_segment(seg)

    def stop_and_save(self) -> Path:
        self._running = False
        path = MEETINGS_DIR / f"{self.transcript.meeting_id}.json"
        import json
        path.write_text(json.dumps(self.transcript.to_dict(), indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        return path


def diarize(audio_path: Path, transcript: Transcript,
            hf_token: Optional[str] = None) -> Transcript:
    """Optional speaker diarization via pyannote. No-op if unavailable."""
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        return transcript
    pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                    use_auth_token=hf_token)
    diar = pipe(str(audio_path))
    for seg in transcript.segments:
        mid = (seg.start + seg.end) / 2
        for turn, _, spk in diar.itertracks(yield_label=True):
            if turn.start <= mid <= turn.end:
                seg.speaker = f"화자 {spk.split('_')[-1]}"
                break
    return transcript
