"""Post-meeting speaker diarization via sherpa-onnx (ONNX, no torch).

Runs on the saved meeting WAV after recording ends: a pyannote segmentation
model + a 3D-Speaker embedding model (both ONNX, CPU) find speaker turns,
then each transcript segment is labelled 화자 N by time overlap.

Models are public (no HF token) and auto-downloaded to the models cache on
first use. Everything degrades gracefully — if sherpa-onnx isn't installed
or a download fails, diarization is skipped and the transcript keeps no
speaker labels.
"""
from __future__ import annotations

import shutil
import tarfile
import tempfile
import traceback
import urllib.request
import wave
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import MODELS_CACHE

Turn = Tuple[float, float, str]

_SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
_EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/"
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
_SEG = MODELS_CACHE / "diar-segmentation.onnx"
_EMB = MODELS_CACHE / "diar-embedding.onnx"


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def models_installed() -> bool:
    return _SEG.exists() and _EMB.exists()


def ensure_models() -> bool:
    """Download the ONNX diarization models if missing. Returns True on success."""
    MODELS_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        if not _SEG.exists():
            with tempfile.TemporaryDirectory() as td:
                tb = Path(td) / "seg.tar.bz2"
                urllib.request.urlretrieve(_SEG_URL, tb)
                with tarfile.open(tb, "r:bz2") as tar:
                    tar.extractall(td)
                found = next(Path(td).rglob("model.onnx"), None)
                if not found:
                    return False
                shutil.copy(found, _SEG)
        if not _EMB.exists():
            urllib.request.urlretrieve(_EMB_URL, _EMB)
        return models_installed()
    except Exception:
        traceback.print_exc()
        return False


def _read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    a = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    return a, sr


def diarize_wav(wav_path: Path, num_speakers: int = -1) -> Optional[List[Turn]]:
    """Speaker turns for the WAV, or None if diarization can't run."""
    try:
        import sherpa_onnx
    except Exception:
        return None
    if not ensure_models():
        return None
    try:
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(_SEG))),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_EMB)),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=int(num_speakers), threshold=0.5),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        sd = sherpa_onnx.OfflineSpeakerDiarization(config)
        samples, _sr = _read_wav(wav_path)
        result = sd.process(samples).sort_by_start_time()
        return [(float(r.start), float(r.end), str(r.speaker)) for r in result]
    except Exception:
        traceback.print_exc()
        return None


def assign_speakers(segments, turns: List[Turn]) -> int:
    """Label each segment with 화자 N based on the overlapping turn.
    Returns the number of distinct speakers found."""
    if not turns:
        return 0
    label_map: dict = {}

    def name(spk: str) -> str:
        if spk not in label_map:
            label_map[spk] = f"화자 {len(label_map) + 1}"
        return label_map[spk]

    for seg in segments:
        mid = (seg.start + seg.end) / 2.0
        chosen = None
        for a, b, spk in turns:
            if a <= mid <= b:
                chosen = spk
                break
        if chosen is None:
            best_ov = 0.0
            for a, b, spk in turns:
                ov = min(seg.end, b) - max(seg.start, a)
                if ov > best_ov:
                    best_ov, chosen = ov, spk
        if chosen is not None:
            seg.speaker = name(chosen)
    return len(label_map)
