"""Audio capture for live meetings.

Mic capture via sounddevice, yielding float32 mono chunks at 16 kHz
(Whisper's native rate). Kept minimal and platform-agnostic; sounddevice
is imported lazily so the package imports without it. System-audio
loopback (recording what you hear) is platform-specific and documented as
an optional enhancement, not required for the mic-based MVP.
"""
from __future__ import annotations

import queue
import wave
from pathlib import Path
from typing import Iterator, Optional

SAMPLE_RATE = 16000
CHANNELS = 1


class MicCapture:
    """Streams microphone audio as float32 numpy chunks.

    Usage:
        cap = MicCapture(block_sec=1.0)
        cap.start()
        for chunk in cap.stream():   # blocks until stop()
            ...
        cap.stop()
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, block_sec: float = 1.0,
                 device: Optional[int] = None):
        self.sample_rate = sample_rate
        self.block_frames = int(sample_rate * block_sec)
        self.device = device
        self._q: "queue.Queue" = queue.Queue()
        self._stream = None
        self._running = False

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # indata: (frames, channels) float32 -> mono 1-D copy
        self._q.put(indata[:, 0].copy())

    def start(self) -> None:
        import sounddevice as sd
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=CHANNELS, dtype="float32",
            blocksize=self.block_frames, device=self.device,
            callback=self._callback)
        self._stream.start()
        self._running = True

    def stream(self) -> Iterator["object"]:
        """Yield audio chunks until stop() is called and the queue drains."""
        while self._running or not self._q.empty():
            try:
                yield self._q.get(timeout=0.5)
            except queue.Empty:
                continue

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class SystemCapture:
    """Captures system audio (speaker loopback) via the `soundcard` library —
    lets you test transcription with any playing audio (video call, YouTube)
    without holding a real meeting. Windows/macOS/Linux loopback supported.

    Exposes the same start()/stream()/stop() interface as MicCapture.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, block_sec: float = 1.0):
        self.sample_rate = sample_rate
        self.block = int(sample_rate * block_sec)
        self._q: "queue.Queue" = queue.Queue()
        self._running = False
        self._thread = None

    def start(self) -> None:
        import threading
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import warnings
        import numpy as np
        import soundcard as sc
        # soundcard emits frequent, harmless "data discontinuity" warnings on
        # loopback (silence / buffer jitter) — silence them.
        try:
            from soundcard import SoundcardRuntimeWarning
            warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
        except Exception:
            warnings.filterwarnings("ignore")
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(str(spk.name), include_loopback=True)
            with mic.recorder(samplerate=self.sample_rate, blocksize=self.block) as rec:
                while self._running:
                    data = rec.record(numframes=self.block)   # (frames, channels)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    self._q.put(np.asarray(mono, dtype="float32"))
        except Exception:
            import traceback
            traceback.print_exc()
            self._running = False

    def stream(self):
        while self._running or not self._q.empty():
            try:
                yield self._q.get(timeout=0.5)
            except queue.Empty:
                continue

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)


def save_wav(path: Path, audio, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write float32 [-1,1] mono samples to a 16-bit PCM WAV."""
    import numpy as np
    pcm = np.clip(np.asarray(audio, dtype="float32"), -1.0, 1.0)
    pcm16 = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return path
