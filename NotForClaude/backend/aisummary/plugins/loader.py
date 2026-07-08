"""Plugin loader + registry.

Scans two sources for `plugin.json` manifests:
  * builtin:  <package>/plugins/builtin/*/plugin.json   (shipped, read-only)
  * user:     ~/.aisummary/plugins/*/plugin.json         (installed)

Enabled state + granted permissions live in ~/.aisummary/plugins/_state.json.
A plugin's manifest is validated; an invalid one is listed with an error and
cannot be enabled. Manifests are pure data — no code is imported or executed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..config import PLUGINS_DIR

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
STATE_PATH = PLUGINS_DIR / "_state.json"

REQUIRED = ("id", "name", "version")


# ---- enabled/permission state ----------------------------------------
def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_state(st: dict) -> None:
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2, ensure_ascii=False),
                          encoding="utf-8")


# ---- manifest scanning + validation ----------------------------------
def _validate(m: dict) -> Optional[str]:
    for k in REQUIRED:
        if not m.get(k):
            return f"필수 항목 누락: {k}"
    if not isinstance(m.get("permissions", {}), dict):
        return "permissions 형식 오류"
    if not isinstance(m.get("contributes", {}), dict):
        return "contributes 형식 오류"
    return None


def _read_manifest(folder: Path) -> Optional[dict]:
    p = folder / "plugin.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(m, dict):
            raise ValueError("최상위가 객체가 아님")
    except Exception as e:
        return {"id": folder.name, "name": folder.name, "version": "?",
                "_dir": str(folder), "_error": f"manifest 파싱 실패: {e}"}
    m["_dir"] = str(folder)
    err = _validate(m)
    if err:
        m["_error"] = err
    return m


def _scan() -> dict:
    """{id: manifest}. User plugins override a builtin with the same id."""
    found: dict = {}
    for base, builtin in ((BUILTIN_DIR, True), (PLUGINS_DIR, False)):
        if not base.exists():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir():
                continue
            m = _read_manifest(folder)
            if not m:
                continue
            m["_builtin"] = builtin
            found[m.get("id") or folder.name] = m
    return found


def _norm_perms(m: dict) -> dict:
    p = m.get("permissions") or {}
    folders = p.get("folders", ["*"])
    if not isinstance(folders, list):
        folders = ["*"]
    return {"folders": folders, "network": bool(p.get("network", False))}


# ---- public API ------------------------------------------------------
def list_plugins() -> list:
    st = _load_state()
    out = []
    for pid, m in _scan().items():
        s = st.get(pid, {})
        out.append({
            "id": pid,
            "name": m.get("name", pid),
            "version": m.get("version", "?"),
            "author": m.get("author", ""),
            "description": m.get("description", ""),
            "builtin": bool(m.get("_builtin", False)),
            "permissions": _norm_perms(m),
            "contributes": m.get("contributes", {}) if not m.get("_error") else {},
            "enabled": bool(s.get("enabled", False)) and not m.get("_error"),
            "error": m.get("_error"),
        })
    out.sort(key=lambda x: (not x["builtin"], x["name"].lower()))
    return out


def get_plugin(pid: str) -> Optional[dict]:
    m = _scan().get(pid)
    if not m:
        return None
    st = _load_state().get(pid, {})
    return {
        "manifest": m,
        "enabled": bool(st.get("enabled", False)) and not m.get("_error"),
        "permissions": _norm_perms(m),
    }


def set_enabled(pid: str, enabled: bool) -> bool:
    plugins = _scan()
    m = plugins.get(pid)
    if not m or m.get("_error"):
        return False
    st = _load_state()
    entry = st.get(pid, {})
    entry["enabled"] = bool(enabled)
    # record the permissions granted at enable time
    entry["granted"] = _norm_perms(m)
    st[pid] = entry
    _save_state(st)
    return True


def persona_of(pid: str) -> Optional[str]:
    """The persona/system instructions of an ENABLED plugin's mode, or None."""
    p = get_plugin(pid)
    if not p or not p["enabled"]:
        return None
    mode = (p["manifest"].get("contributes") or {}).get("mode") or {}
    return mode.get("persona")


def folders_of(pid: str) -> Optional[list]:
    """Granted folder scope of an ENABLED plugin (['*'] = unrestricted)."""
    p = get_plugin(pid)
    if not p or not p["enabled"]:
        return None
    return p["permissions"].get("folders", ["*"])


def uninstall(pid: str) -> bool:
    """Remove a user-installed plugin (builtin plugins can't be removed)."""
    import shutil
    m = _scan().get(pid)
    if not m or m.get("_builtin"):
        return False
    folder = Path(m.get("_dir", ""))
    try:
        # safety: only delete inside the user plugins dir
        if PLUGINS_DIR in folder.resolve().parents or folder.parent == PLUGINS_DIR:
            shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        return False
    st = _load_state()
    if pid in st:
        st.pop(pid, None)
        _save_state(st)
    return True


def plugins_dir() -> str:
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return str(PLUGINS_DIR)
