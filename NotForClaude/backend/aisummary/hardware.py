"""Hardware detection + automatic model/tier selection.

Runs at install time (and on demand). Detects RAM, CPU, and GPU/VRAM, then
picks a TierProfile. The user can override the result in the UI.

Only psutil is a hard dependency. GPU detection degrades gracefully:
NVIDIA via `nvidia-smi`, Apple Silicon via platform, otherwise "none".
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is in requirements
    psutil = None

from .config import TIERS, TierProfile


@dataclass
class GPUInfo:
    vendor: str          # "nvidia" | "apple" | "amd" | "none"
    name: str
    vram_gb: float


@dataclass
class HardwareInfo:
    os: str
    arch: str
    cpu_model: str
    physical_cores: int
    logical_cores: int
    ram_gb: float
    gpu: GPUInfo

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def _detect_nvidia() -> Optional[GPUInfo]:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        ).strip()
        # first GPU line
        name, mem = [x.strip() for x in out.splitlines()[0].split(",")]
        return GPUInfo(vendor="nvidia", name=name, vram_gb=round(float(mem) / 1024, 1))
    except Exception:
        return None


def _detect_apple() -> Optional[GPUInfo]:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        # Apple Silicon uses unified memory; VRAM ~= a large share of RAM.
        ram = psutil.virtual_memory().total / 1e9 if psutil else 16.0
        return GPUInfo(vendor="apple", name="Apple Silicon (unified)",
                       vram_gb=round(ram * 0.7, 1))
    return None


def detect_gpu() -> GPUInfo:
    for detector in (_detect_nvidia, _detect_apple):
        info = detector()
        if info:
            return info
    return GPUInfo(vendor="none", name="CPU only", vram_gb=0.0)


def detect_hardware() -> HardwareInfo:
    ram_gb = (psutil.virtual_memory().total / 1e9) if psutil else 8.0
    phys = (psutil.cpu_count(logical=False) or 2) if psutil else 2
    logi = (psutil.cpu_count(logical=True) or 4) if psutil else 4
    return HardwareInfo(
        os=platform.system(),
        arch=platform.machine(),
        cpu_model=platform.processor() or "unknown",
        physical_cores=phys,
        logical_cores=logi,
        ram_gb=round(ram_gb, 1),
        gpu=detect_gpu(),
    )


def select_tier(hw: HardwareInfo) -> TierProfile:
    """Map detected hardware to a tier. See ARCHITECTURE.md §5."""
    vram = hw.gpu.vram_gb
    ram = hw.ram_gb

    # High: strong dGPU or a lot of unified/system memory.
    if vram >= 8 or ram >= 24:
        return TIERS["high"]
    # Mid: modern laptop, 6GB+ VRAM or 12GB+ RAM.
    if vram >= 6 or ram >= 12:
        return TIERS["mid"]
    # Low: everything else (thin/older laptops, CPU-only, <12GB RAM).
    return TIERS["low"]


def recommend() -> dict:
    """One-call helper for the installer: detect + recommend + explain."""
    hw = detect_hardware()
    tier = select_tier(hw)
    reason = _explain(hw, tier)
    return {
        "hardware": hw.as_dict(),
        "recommended_tier": tier.name,
        "profile": asdict(tier),
        "reason": reason,
        "all_tiers": {k: asdict(v) for k, v in TIERS.items()},
    }


def _explain(hw: HardwareInfo, tier: TierProfile) -> str:
    g = hw.gpu
    gpu_txt = f"{g.name} ({g.vram_gb}GB VRAM)" if g.vendor != "none" else "GPU 없음"
    return (
        f"감지: RAM {hw.ram_gb}GB, {gpu_txt}, {hw.physical_cores}코어. "
        f"→ '{tier.name}' 티어 추천: LLM {tier.llm_model}, "
        f"임베딩 {tier.embed_model}, STT whisper-{tier.stt_model}."
    )


if __name__ == "__main__":
    import json
    print(json.dumps(recommend(), indent=2, ensure_ascii=False))
