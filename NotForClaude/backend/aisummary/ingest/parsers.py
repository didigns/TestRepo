"""Document parsers. Return a list of (page_number, text) blocks so that
downstream chunking can preserve page-level citation spans.

Heavy parsers (pymupdf, python-docx) are imported lazily so the package
still imports on a machine that hasn't installed every optional dep yet.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

Block = Tuple[int, str]  # (page, text)

SUPPORTED = {".pdf", ".docx", ".pptx", ".txt", ".md", ".markdown",
             ".html", ".htm", ".csv", ".hwp", ".hwpx"}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED


def parse(path: Path) -> List[Block]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".pptx":
        return _parse_pptx(path)
    if ext in {".html", ".htm"}:
        return _parse_html(path)
    if ext == ".hwpx":
        return _parse_hwpx(path)
    if ext == ".hwp":
        return _parse_hwp(path)
    # plain text family
    return [(1, path.read_text(encoding="utf-8", errors="replace"))]


def _parse_pdf(path: Path) -> List[Block]:
    import fitz  # pymupdf
    blocks: List[Block] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if text:
                blocks.append((i, text))
    return blocks


def _parse_docx(path: Path) -> List[Block]:
    import docx
    d = docx.Document(str(path))
    text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
    return [(1, text)]


def _parse_pptx(path: Path) -> List[Block]:
    from pptx import Presentation
    prs = Presentation(str(path))
    blocks: List[Block] = []
    for i, slide in enumerate(prs.slides, start=1):
        parts = [sh.text for sh in slide.shapes if sh.has_text_frame and sh.text.strip()]
        if parts:
            blocks.append((i, "\n".join(parts)))
    return blocks


def _parse_html(path: Path) -> List[Block]:
    try:
        import trafilatura
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = trafilatura.extract(raw) or ""
    except ImportError:
        import re
        text = re.sub(r"<[^>]+>", " ", path.read_text(encoding="utf-8", errors="replace"))
    return [(1, text.strip())]


def _parse_hwpx(path: Path) -> List[Block]:
    """HWPX (한글 신형, ZIP+XML) — dependency-free. Text lives in
    Contents/section*.xml inside <hp:t> runs."""
    import re
    import zipfile
    import html as _html
    blocks: List[Block] = []
    with zipfile.ZipFile(path) as z:
        sections = sorted(n for n in z.namelist()
                          if re.match(r"Contents/section\d+\.xml", n))
        for i, name in enumerate(sections or [], start=1):
            xml = z.read(name).decode("utf-8", "ignore")
            runs = re.findall(r"<hp:t\b[^>]*>(.*?)</hp:t>", xml, re.DOTALL)
            text = "".join(_html.unescape(re.sub(r"<[^>]+>", "", r)) for r in runs)
            # paragraph breaks
            text = re.sub(r"</hp:p>", "\n", text)
            if text.strip():
                blocks.append((i, text.strip()))
    return blocks or [(1, "")]


def _parse_hwp(path: Path) -> List[Block]:
    """HWP v5 (한글 구형, OLE binary). Prefer gethwp; fall back to the
    embedded PrvText preview stream via olefile."""
    try:
        import gethwp
        text = gethwp.read_hwp(str(path)) or ""
        if text.strip():
            return [(1, text.strip())]
    except Exception:
        pass
    try:
        import olefile
        ole = olefile.OleFileIO(str(path))
        if ole.exists("PrvText"):
            data = ole.openstream("PrvText").read()
            return [(1, data.decode("utf-16", "ignore").strip())]
    except Exception:
        pass
    return [(1, "")]
