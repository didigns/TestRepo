"""확장자 → 모델 라우팅. pdf/이미지는 비전(MiniCPM-V), 그 외는 텍스트(Gemma)."""
import os

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
# 텍스트로 추출 가능한 문서
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm",
             ".docx", ".xlsx", ".pptx", ".hwp", ".rtf", ".py", ".js", ".ts"}

VISION = "vision"   # MiniCPM-V
TEXT = "text"       # Gemma


def route(path):
    """파일 경로 → (model_key, ext). 지원 안 하면 (None, ext)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS or ext in IMAGE_EXTS:
        return VISION, ext
    if ext in TEXT_EXTS:
        return TEXT, ext
    return None, ext


def is_supported(path):
    key, _ = route(path)
    return key is not None
