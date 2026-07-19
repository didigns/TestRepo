"""Source-document preview: render a real page + locate cited text.

Powers the right-hand preview panel. For PDFs, renders the page to PNG via
pymupdf and returns normalized highlight rectangles for the cited passage.
For other file types, returns plain text. Only files that are actually
indexed can be served (path allow-list) to prevent arbitrary file reads.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

from ..config import Settings, DATA_DIR

STATE_PATH = DATA_DIR / "ingest_state.json"


def _state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def is_indexed(path: str) -> bool:
    """Security guard — only indexed files may be previewed."""
    return path in _state()


def _norm_state(state: dict) -> dict:
    """State keyed by normalized path, for cross-referencing disk files."""
    return {os.path.normpath(k): v for k, v in state.items()}


def _disk_files(folder: str) -> List[Path]:
    """All indexable files physically present under a folder (recursive).

    This lets the folder view show files *before* indexing finishes — the
    listing is sourced from disk, and index status is looked up separately.
    """
    from ..ingest import parsers
    root = Path(folder)
    if not root.exists():
        return []
    out: List[Path] = []
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file() and parsers.is_supported(p):
                    out.append(p)
            except OSError:
                continue
    except Exception:
        pass
    return out


def kb_summary(settings: Settings) -> dict:
    """Per-folder rollup. `files` = total indexable files on disk,
    `indexed` = how many are already in the index (green dots)."""
    ns = _norm_state(_state())
    folders = []
    for folder in settings.watch_folders:
        f = os.path.normpath(folder)
        disk = _disk_files(folder)
        indexed = chunks = 0
        for p in disk:
            meta = ns.get(os.path.normpath(str(p)))
            if meta:
                indexed += 1
                chunks += int(meta.get("chunks", 0))
        folders.append({
            "path": folder,
            "name": os.path.basename(f.rstrip("/\\")) or folder,
            "files": len(disk), "indexed": indexed, "chunks": chunks,
        })
    return {
        "folders": folders,
        "total_files": sum(fo["files"] for fo in folders),
        "total_indexed": sum(fo["indexed"] for fo in folders),
        "total_chunks": sum(int(v.get("chunks", 0)) for v in _state().values()),
    }


def list_files(settings: Settings, folder: str) -> List[dict]:
    """All indexable files under a folder (on disk), each flagged with its
    index status. Unindexed files appear immediately with a gray dot; once
    indexed they carry chunk counts and turn green in the UI."""
    ns = _norm_state(_state())
    out = []
    for p in _disk_files(folder):
        meta = ns.get(os.path.normpath(str(p)))
        out.append({
            "name": p.name, "path": str(p),
            "indexed": bool(meta),
            "chunks": int(meta.get("chunks", 0)) if meta else 0,
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


def indexed_files() -> List[dict]:
    """Every indexed file as [{name, path}] — powers @-mention file tagging."""
    out = [{"name": os.path.basename(p), "path": p} for p in _state().keys()]
    out.sort(key=lambda x: x["name"].lower())
    return out


# Rendering a PDF page (open + rasterize + PNG encode) is the slow part of
# preview, so cache the result in-process keyed by (path, mtime, page, dpi).
# Repeat views and prefetched pages then return instantly. Bounded LRU.
from collections import OrderedDict as _OrderedDict

_PAGE_CACHE: "_OrderedDict[tuple, bytes]" = _OrderedDict()
_PAGE_CACHE_MAX = 64
_PC_CACHE: dict = {}  # (path, mtime) -> page_count


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def render_page_png(path: str, page: int, dpi: int = 130) -> Optional[bytes]:
    """Render a PDF page (1-based) to PNG bytes. None if not a PDF. Cached by
    (path, mtime, page, dpi) so repeat/prefetched views are instant."""
    if not path.lower().endswith(".pdf"):
        return None
    key = (path, _mtime(path), page, dpi)
    hit = _PAGE_CACHE.get(key)
    if hit is not None:
        _PAGE_CACHE.move_to_end(key)
        return hit
    import fitz  # pymupdf
    with fitz.open(path) as doc:
        _PC_CACHE[(path, key[1])] = doc.page_count  # warm page_count for free
        idx = max(0, min(page - 1, doc.page_count - 1))
        pix = doc[idx].get_pixmap(dpi=dpi)
        png = pix.tobytes("png")
    _PAGE_CACHE[key] = png
    _PAGE_CACHE.move_to_end(key)
    while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
        _PAGE_CACHE.popitem(last=False)
    return png


def page_count(path: str) -> int:
    if not path.lower().endswith(".pdf"):
        return 1
    key = (path, _mtime(path))
    cached = _PC_CACHE.get(key)
    if cached is not None:
        return cached
    import fitz
    with fitz.open(path) as doc:
        _PC_CACHE[key] = doc.page_count
        return _PC_CACHE[key]


def find_highlights(path: str, page: int, query: str) -> List[dict]:
    """Normalized [0,1] rects of the cited text on the page (PDF only)."""
    if not path.lower().endswith(".pdf") or not query.strip():
        return []
    import fitz
    with fitz.open(path) as doc:
        idx = max(0, min(page - 1, doc.page_count - 1))
        pg = doc[idx]
        W, H = pg.rect.width or 1, pg.rect.height or 1
        rects = []
        # try progressively shorter fragments until something matches
        for frag in _fragments(query):
            found = pg.search_for(frag)
            if found:
                rects = found
                break
        return [{"x0": r.x0 / W, "y0": r.y0 / H, "x1": r.x1 / W, "y1": r.y1 / H}
                for r in rects]


def _fragments(text: str) -> List[str]:
    t = " ".join(text.split())
    out = [t]
    # first sentence
    for sep in (". ", ". ", "다. ", "。"):
        if sep in t:
            out.append(t.split(sep)[0])
            break
    out.append(t[:40])
    out.append(t[:20])
    seen, uniq = set(), []
    for f in out:
        f = f.strip()
        if f and f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def file_text(path: str, max_chars: int = 200000) -> str:
    """Plain text for non-PDF preview (docx/txt/md/etc.)."""
    from ..ingest import parsers
    try:
        blocks = parsers.parse(Path(path))
        return "\n\n".join(t for _, t in blocks)[:max_chars]
    except Exception:
        return ""


def _locate(text: str, query: str) -> Optional[tuple]:
    """Find the cited passage in `text`, tolerant of whitespace/newline
    differences. Returns (start, end) char offsets or None."""
    if not query.strip():
        return None
    for frag in _fragments(query):
        i = text.find(frag)               # exact first
        if i >= 0:
            return (i, i + len(frag))
        words = frag.split()              # whitespace-insensitive fallback
        if len(words) >= 2:
            pat = re.compile(r"\s+".join(re.escape(w) for w in words))
            m = pat.search(text)
            if m:
                return (m.start(), m.end())
    return None


def text_preview(path: str, query: str = "", window: int = 3500) -> dict:
    """Text for non-PDF preview with the cited span located and windowed
    so the highlight is always visible even in long documents."""
    full = file_text(path)
    hit = _locate(full, query)
    if not hit:
        return {"text": full[:6000], "ranges": []}
    a, b = hit
    start, end = max(0, a - window), min(len(full), b + window)
    text = full[start:end]
    ra = [a - start, b - start]
    if start > 0:                         # leading ellipsis shifts offsets
        text = "…" + text
        ra = [ra[0] + 1, ra[1] + 1]
    if end < len(full):
        text = text + "…"
    return {"text": text, "ranges": [ra]}
