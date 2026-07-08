"""Chat session persistence + user-created folders (drag-drop organization).

Each conversation is saved as one JSON record under SESSIONS_DIR. By default a
session has ``folder_id = None`` and shows at the root of the chat list. Users
create folders (stored in ``_folders.json``) and move sessions between them via
drag-and-drop in the UI. Everything is 100% local, mirroring meetings/store.py.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import SESSIONS_DIR

FOLDERS_PATH = SESSIONS_DIR / "_folders.json"


def _ensure() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> float:
    return time.time()


def _spath(sid: str) -> Path:
    return SESSIONS_DIR / f"{sid}.json"


# ---- folders ---------------------------------------------------------
def load_folders() -> list:
    _ensure()
    if FOLDERS_PATH.exists():
        try:
            data = json.loads(FOLDERS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_folders(folders: list) -> None:
    _ensure()
    FOLDERS_PATH.write_text(
        json.dumps(folders, indent=2, ensure_ascii=False), encoding="utf-8")


def create_folder(name: str = "새 폴더") -> dict:
    folders = load_folders()
    folder = {
        "folder_id": "f" + uuid.uuid4().hex[:12],
        "name": (name or "새 폴더").strip()[:60] or "새 폴더",
        "created_at": _now(),
        "order": len(folders),
    }
    folders.append(folder)
    _save_folders(folders)
    return folder


def rename_folder(fid: str, name: str) -> bool:
    folders = load_folders()
    hit = False
    for f in folders:
        if f.get("folder_id") == fid:
            f["name"] = (name or "").strip()[:60] or f.get("name", "폴더")
            hit = True
    if hit:
        _save_folders(folders)
    return hit


def delete_folder(fid: str) -> bool:
    """Remove a folder; its sessions fall back to the root (folder_id=None)."""
    folders = load_folders()
    kept = [f for f in folders if f.get("folder_id") != fid]
    if len(kept) == len(folders):
        return False
    _save_folders(kept)
    for rec in _records():
        if rec.get("folder_id") == fid:
            rec["folder_id"] = None
            _write(rec)
    return True


# ---- sessions --------------------------------------------------------
def _records():
    _ensure()
    for p in SESSIONS_DIR.glob("s*.json"):
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue


def _write(rec: dict) -> None:
    _ensure()
    _spath(rec["session_id"]).write_text(
        json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")


def create_session(title: str = "새 대화", folder_id: Optional[str] = None) -> dict:
    sid = "s" + uuid.uuid4().hex[:12]
    ts = _now()
    if folder_id is not None and not any(
            f.get("folder_id") == folder_id for f in load_folders()):
        folder_id = None
    rec = {
        "session_id": sid,
        "title": (title or "새 대화").strip()[:80] or "새 대화",
        "folder_id": folder_id,
        "created_at": ts,
        "updated_at": ts,
        "messages": [],
    }
    _write(rec)
    return rec


def get_session(sid: str) -> Optional[dict]:
    p = _spath(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def append_message(sid: str, role: str, content: str,
                   citations: Optional[list] = None) -> Optional[dict]:
    rec = get_session(sid)
    if not rec:
        return None
    rec.setdefault("messages", []).append({
        "role": role, "content": content or "", "citations": citations or [],
    })
    rec["updated_at"] = _now()
    _write(rec)
    return rec


def rename_session(sid: str, title: str) -> bool:
    rec = get_session(sid)
    if not rec:
        return False
    rec["title"] = (title or "").strip()[:80] or rec.get("title", "대화")
    rec["updated_at"] = _now()
    _write(rec)
    return True


def move_session(sid: str, folder_id: Optional[str]) -> bool:
    """Move a session into a folder (folder_id) or to the root (None)."""
    rec = get_session(sid)
    if not rec:
        return False
    if folder_id is not None and not any(
            f.get("folder_id") == folder_id for f in load_folders()):
        return False
    rec["folder_id"] = folder_id
    rec["updated_at"] = _now()
    _write(rec)
    return True


def delete_session(sid: str) -> bool:
    p = _spath(sid)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False


def list_all() -> dict:
    """Return {folders, sessions}. Sessions carry folder_id (None=root),
    a first-user-message preview, and are ordered newest-updated first."""
    folders = sorted(load_folders(), key=lambda f: (f.get("order", 0),
                                                     f.get("created_at", 0)))
    sessions = []
    for rec in _records():
        msgs = rec.get("messages", []) or []
        preview = ""
        for m in msgs:
            if m.get("role") == "user":
                preview = (m.get("content") or "")[:80]
                break
        sessions.append({
            "session_id": rec.get("session_id"),
            "title": rec.get("title", "대화"),
            "folder_id": rec.get("folder_id"),
            "updated_at": rec.get("updated_at") or rec.get("created_at") or 0,
            "preview": preview,
            "count": len(msgs),
        })
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return {"folders": folders, "sessions": sessions}
