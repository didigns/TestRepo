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
    # read_only 모드는 시트 메타데이터의 dimension을 그대로 믿는다.
    # 서식만 입힌 초대형 범위(열 전체 스타일 등)가 있으면 수백만 개의
    # 빈 행을 순회하며 사실상 멈춘다(진행 이벤트도 없어 UI가 '준비 중'에 고정).
    # reset_dimensions()로 실제 데이터 기준으로 순회하고, 그래도 병리적인
    # 파일에서 반드시 끝나도록 빈 행 연속·총량 상한을 둔다.
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    total = 0
    try:
        for ws in wb.worksheets:
            try:
                ws.reset_dimensions()
            except Exception:
                pass
            parts.append(f"[{ws.title}]")
            empty_streak = 0
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if not cells:
                    empty_streak += 1
                    if empty_streak >= 10_000:  # 값 없는 행 폭주 → 이 시트 포기
                        break
                    continue
                empty_streak = 0
                line = "\t".join(cells)
                parts.append(line)
                total += len(line)
                if total >= MAX_TEXT_CHARS:  # 상한 도달 → 즉시 종료
                    return "\n".join(parts)
    finally:
        wb.close()  # read_only 모드는 명시적으로 닫아야 파일 핸들이 풀린다
    return "\n".join(parts)


# HWP 5.0 본문 문자 중 8워드(16바이트)를 차지하는 인라인/확장 컨트롤 코드.
# 1워드 컨트롤: 0, 10(줄바꿈), 13(문단끝), 24~31. 나머지 <32는 8워드.
_HWP_ONE_UNIT = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def _hwp_para_text(buf):
    """HWPTAG_PARA_TEXT 레코드(UTF-16LE + 컨트롤)를 순수 텍스트로."""
    import struct
    n = len(buf) // 2
    units = struct.unpack_from("<%dH" % n, buf)
    out = []
    i = 0
    while i < n:
        cu = units[i]
        if cu in (10, 13):
            out.append("\n")
            i += 1
        elif cu < 32:
            i += 1 if cu in _HWP_ONE_UNIT else 8  # 컨트롤 페이로드 건너뜀
        elif 0xD800 <= cu <= 0xDBFF and i + 1 < n and 0xDC00 <= units[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((cu - 0xD800) << 10) + (units[i + 1] - 0xDC00)))
            i += 2
        elif 0xD800 <= cu <= 0xDFFF:
            i += 1  # 짝 잃은 서로게이트는 버림
        else:
            out.append(chr(cu))
            i += 1
    return "".join(out)


def _read_hwp(path):
    """HWP 5.0(복합문서) 본문 추출. BodyText 레코드 파싱, 실패 시 PrvText."""
    try:
        import olefile
    except ImportError:
        return None
    import re as _re
    import struct
    import zlib
    try:
        ole = olefile.OleFileIO(path)
    except Exception:
        return ""  # HWP지만 복합문서가 아님(hwpx 등) → 본문 없음 처리
    try:
        compressed = True
        try:
            hdr = ole.openstream("FileHeader").read()
            compressed = bool(hdr[36] & 1)
        except Exception:
            pass
        sections = sorted(
            (e for e in ole.listdir() if e and e[0] == "BodyText"),
            key=lambda e: int(_re.sub(r"\D", "", e[-1]) or 0))
        parts = []
        total = 0
        for entry in sections:
            try:
                data = ole.openstream(entry).read()
                if compressed:
                    data = zlib.decompress(data, -15)
            except Exception:
                continue
            i, n = 0, len(data)
            while i + 4 <= n:
                (h,) = struct.unpack_from("<I", data, i)
                tag = h & 0x3FF
                size = (h >> 20) & 0xFFF
                i += 4
                if size == 0xFFF:  # 확장 크기
                    if i + 4 > n:
                        break
                    (size,) = struct.unpack_from("<I", data, i)
                    i += 4
                if tag == 67 and i + size <= n:  # HWPTAG_PARA_TEXT
                    t = _hwp_para_text(data[i:i + size])
                    if t.strip():
                        parts.append(t)
                        total += len(t)
                        if total >= MAX_TEXT_CHARS:
                            return "\n".join(parts)
                i += size
        body = "\n".join(parts)
        if body.strip():
            return body
        # 폴백: 미리보기 텍스트(앞부분만이라도)
        try:
            if ole.exists("PrvText"):
                return ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
        except Exception:
            pass
        return ""
    finally:
        try:
            ole.close()
        except Exception:
            pass


def _read_pdf_text(path):
    # 1순위: PyMuPDF(fitz) — C 기반이라 빠르고 손상 PDF에 강하다.
    # pypdf는 복잡한 PDF에서 페이지당 수 분씩 걸리거나
    # "Impossible to decode XFormObject" 경고를 stderr에 쏟아내는 일이 잦다.
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        try:
            parts = []
            total = 0
            for page in doc:
                t = page.get_text() or ""
                if t.strip():
                    parts.append(t)
                    total += len(t)
                    if total >= MAX_TEXT_CHARS:
                        break
            return "\n".join(parts)
        finally:
            doc.close()
    except ImportError:
        pass
    except Exception:
        pass  # 손상/비정상 PDF → pypdf 폴백
    # 2순위(폴백): pypdf — 느리지만 fitz 미설치/실패 시 최소한의 추출은 보장
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # XFormObject 등 경고 스팸 억제
    reader = PdfReader(path)
    parts = []
    total = 0
    for page in reader.pages:
        t = page.extract_text() or ""
        if t.strip():
            parts.append(t)
            total += len(t)
            if total >= MAX_TEXT_CHARS:
                break
    return "\n".join(parts)


def _pdf_to_images(path, max_pages):
    """PyMuPDF(fitz)로 페이지를 PNG로 렌더링. max_pages<=0 이면 전체."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    imgs = []
    tmpdir = tempfile.mkdtemp(prefix="levacache_")
    doc = fitz.open(path)
    for i, page in enumerate(doc):
        if max_pages > 0 and i >= max_pages:
            break
        pix = page.get_pixmap(dpi=144)
        out = os.path.join(tmpdir, f"page_{i+1}.png")
        pix.save(out)
        imgs.append(out)
    doc.close()
    return imgs or None


def prepare(path, ext, *, pdf_max_pages=0):
    ext = ext.lower()
    # 이미지 → 그대로 비전 입력
    from .router import AUDIO_EXTS, IMAGE_EXTS
    if ext in IMAGE_EXTS:
        return {"kind": "images", "images": [path], "text": "", "note": "image"}
    # 음성 → 1단계에서 Whisper 전사(파이프라인이 transcribe 훅으로 처리)
    if ext in AUDIO_EXTS:
        return {"kind": "audio", "images": [], "text": "", "note": "audio"}

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
    elif ext == ".hwp":
        # 바이너리 복합문서 — _read_txt로 읽으면 깨진 문자가 임베딩에 들어간다
        text = _read_hwp(path)
    else:
        text = _read_txt(path)
    if text is None:
        dep = {".docx": "python-docx", ".xlsx": "openpyxl",
               ".hwp": "olefile"}.get(ext, ext)
        return {"kind": "text", "images": [], "text": "",
                "note": f"{ext} 처리 라이브러리 없음 (pip install {dep})"}
    return {"kind": "text", "images": [], "text": _truncate(text), "note": "text"}
