"""Mediated per-plugin local storage (host API `storage:local`).

Each plugin gets an isolated directory under ~/.aisummary/plugin-data/<pid>/.
Keys map to <key>.json files. Plugins never receive filesystem paths — only
key/value calls through the host — so a plugin cannot read another plugin's
data or reach anywhere else on disk. Combined with WASM having no network
import, a T2 plugin can persist data but never exfiltrate it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..config import PLUGIN_DATA_DIR


def _safe(part: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.\-]", "_", part or "")[:96]


def _dir(pid: str) -> Path:
    d = PLUGIN_DATA_DIR / _safe(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(pid: str, key: str, value: Any) -> None:
    (_dir(pid) / (_safe(key) + ".json")).write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8")


def load(pid: str, key: str) -> Optional[Any]:
    p = _dir(pid) / (_safe(key) + ".json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete(pid: str, key: str) -> bool:
    p = _dir(pid) / (_safe(key) + ".json")
    if p.exists():
        p.unlink()
        return True
    return False


def keys(pid: str) -> list:
    return sorted(f.stem for f in _dir(pid).glob("*.json"))
