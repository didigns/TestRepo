"""파일에서 모델 입력을 준비한다.
- 텍스트 계열: 본문 문자열 추출 (best-effort, 선택적 의존성)
- 이미지: 경로 그대로 (비전 모델에 이미지로 전달)
- PDF: PyMuPDF가 있으면 앞쪽 페이지를 이미지로 렌더링(비전), 없으면 pypdf로 텍스트 추출
반환: dict(kind='text'|'images', text=..., images=[png경로...], note=...)
"""
import hashlib
import os
import tempfile

# 본문 상한. 과거 12,000자였으나 이는 긴 문서(소설 등)의 98% 이상을
# 임베딩에서 누락시켜 RAG 검색을 무력화했다(다크메이지 사례: 89만 자 중 1.3%만 임베딩).
# 프롬프트 폭주 방지는 하류에서 이미 보장된다:
#   - 키워드(3단계): run_text_extraction 이 chunkChars×maxChunks(기본 24,000자)로 자체 제한
#   - 채팅 주입: _augment 예산 7,000자 상한 + 매칭 청크 기준 주입
# 여기서는 병리적 초대형 파일만 막는 안전 상한으로만 쓴다.
MAX_TEXT_CHARS = 2_000_000


def file_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _truncate(text):
    text = (text or "").strip()
    return text[:MAX_TEXT_CHARS]


def _read_txt(path):
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _read_docx(path):
    try:
        import docx  # python-docx
    except ImportError:
        return None
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append("\t".join(c.text for c in row.cells))
    return "\n".join(parts)


def _read_xlsx(path):
    try:
        import openpyxl
    except ImportError:
        return None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[{ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _read_pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(path)
    parts = []
    for page in reader.pages:
        t = page.extract_text() or ""
        if t.strip():
            parts.append(t)
    return "\n".join(parts)


def _pdf_to_images(path, max_pages):
    """PyMuPDF(fitz)로 앞쪽 페이지를 PNG로 렌더링. 없으면 None."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    imgs = []
    tmpdir = tempfile.mkdtemp(prefix="levacache_")
    doc = fitz.open(path)
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(dpi=144)
        out = os.path.join(tmpdir, f"page_{i+1}.png")
        pix.save(out)
        imgs.append(out)
    doc.close()
    return imgs or None


def prepare(path, ext, *, pdf_max_pages=5):
    ext = ext.lower()
    # 이미지 → 그대로 비전 입력
    from .router import IMAGE_EXTS
    if ext in IMAGE_EXTS:
        return {"kind": "images", "images": [path], "text": "", "note": "image"}

    if ext == ".pdf":
        # 텍스트가 있으면 그대로 추출해 텍스트 모델(Gemma)로 처리
        text = _read_pdf_text(path)
        if text and text.strip():
            return {"kind": "text", "images": [], "text": _truncate(text), "note": "pdf->text"}
        # 텍스트가 없으면(스캔본) 이미지로 렌더링해 비전 모델(MiniCPM) OCR
        imgs = _pdf_to_images(path, pdf_max_pages)
        if imgs:
            return {"kind": "images", "images": imgs, "text": "", "note": "pdf->image(scanned)"}
        # 텍스트도 이미지도 못 얻음 → 원인을 note에 명시
        if text is None and imgs is None:
            note = "PDF 처리 라이브러리 없음 (pip install pypdf PyMuPDF)"
        elif text is None:
            note = "pypdf 미설치로 텍스트 추출 불가 (pip install pypdf)"
        else:
            note = "PDF에서 텍스트를 찾지 못함 (스캔본이면 pip install PyMuPDF)"
        return {"kind": "text", "images": [], "text": "", "note": note}

    # 텍스트 계열
    if ext == ".docx":
        text = _read_docx(path)
    elif ext == ".xlsx":
        text = _read_xlsx(path)
    else:
        text = _read_txt(path)
    if text is None:
        dep = {".docx": "python-docx", ".xlsx": "openpyxl"}.get(ext, ext)
        return {"kind": "text", "images": [], "text": "",
                "note": f"{ext} 처리 라이브러리 없음 (pip install {dep})"}
    return {"kind": "text", "images": [], "text": _truncate(text), "note": "text"}
