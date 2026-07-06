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


def kb_summary(settings: Settings) -> dict:
    state = _state()
    folders = []
    for folder in settings.watch_folders:
        f = os.path.normpath(folder)
        files = [p for p in state if os.path.normpath(p).startswith(f)]
        chunks = sum(int(state[p].get("chunks", 0)) for p in files)
        folders.append({"path": folder, "name": os.path.basename(f.rstrip("/\\")) or folder,
                        "files": len(files), "chunks": chunks})
    return {
        "folders": folders,
        "total_files": len(state),
        "total_chunks": sum(int(v.get("chunks", 0)) for v in state.values()),
    }


def list_files(settings: Settings, folder: str) -> List[dict]:
    """Indexed files under a watched folder (name, path, chunk count)."""
    state = _state()
    f = os.path.normpath(folder)
    out = []
    for p, meta in state.items():
        if os.path.normpath(p).startswith(f):
            out.append({"name": os.path.basename(p), "path": p,
                        "chunks": int(meta.get("chunks", 0))})
    out.sort(key=lambda x: x["name"].lower())
    return out


def render_page_png(path: str, page: int, dpi: int = 130) -> Optional[bytes]:
    """Render a PDF page (1-based) to PNG bytes. None if not a PDF."""
    if not path.lower().endswith(".pdf"):
        return None
    import fitz  # pymupdf
    with fitz.open(path) as doc:
        idx = max(0, min(page - 1, doc.page_count - 1))
        pix = doc[idx].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def page_count(path: str) -> int:
    if not path.lower().endswith(".pdf"):
        return 1
    import fitz
    with fitz.open(path) as doc:
        return doc.page_count


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
